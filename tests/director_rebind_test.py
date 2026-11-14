#!/usr/bin/env python3
"""SYRD-738: the operator's rebind of a Director held by a provider safety hold, through every seam it has.

The board here is a stand-in that answers the reads root makes over its proven socket -- the SYRD-737 packet (a real
one, built and validated by recovery_packet itself), the declared workflow and the runtime assignments -- and records
the root-only calls the command makes through the database owner, enforcing rebind_declared_pane_roles' compare-and-
swap. tmux and the role start are role_runtime_test's stand-ins: sessions exist until killed, and a start records the
runtime the configuration named at that moment. The production board's own functions are proved in
director_rebind_live_test.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import director_rebind as dr, runtime_handover, team_launcher  # noqa: E402
from scripts.ticket_board import recovery_packet as rp  # noqa: E402

import recovery_packet_test as rpt  # noqa: E402
from role_runtime_test import RuntimeRunner, _fake_start, _workflow_document, _write_config  # noqa: E402

CHECKS = 0
HOLD = 4
REASON = "the claude account refused the Director on safety grounds; moving coordination to codex"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def packet(**changes: Any) -> dict:
    rows = rpt.raw(project="porter", **changes)
    return rp.build(rows)


class Board:
    """The reads root makes, and the root-only calls, of one board."""

    def __init__(self, *, built: dict | None = None, refuse: str = "", refuse_revert: bool = False,
                 fail_reads_after_rebind: int = 0, commit_then_raise: bool = False) -> None:
        self.fail_reads_after_rebind = fail_reads_after_rebind  # reads of the workflow that fail once it says codex
        self.commit_then_raise = commit_then_raise  # the forward rebind commits, and then psql reports an error
        self.document = _workflow_document()
        self.revision = 7
        self.packet = built or packet()
        self.calls: list[tuple[str, tuple]] = []
        self.refuse = refuse  # a root-only function that raises
        self.refuse_revert = refuse_revert
        self.registered: dict | None = {"runtime": "claude", "actual_target": "porter-director:0.0",
                                        "process_pid": 999999, "process_start_time": 1, "process_uid": 1000,
                                        "generation": 3, "worktree": "/should/not/be/kept"}

    def declared(self) -> str:
        return next(r["runtime"] for r in self.document["roles"] if r["name"] == "director")

    def get(self, path: str) -> Any:
        if path.startswith(rp.PATH):
            return copy.deepcopy(self.packet)
        if path == "/api/workflow":
            if self.fail_reads_after_rebind and self.declared() == "codex":
                self.fail_reads_after_rebind -= 1
                raise TimeoutError("the board socket timed out")
            return {"document": copy.deepcopy(self.document), "revision": self.revision}
        if path == "/api/runtime-assignments":
            return {"assignments": {"director": dict(self.registered)} if self.registered else {}}
        raise AssertionError(f"unexpected read {path}")

    def call(self, function: str, *args: tuple[Any, str]) -> Any:
        values = tuple(value for value, _type in args)
        self.calls.append((function.removeprefix("ticket_board."), values))
        name = function.removeprefix("ticket_board.")
        if name == self.refuse:
            raise RuntimeError(f"ERROR:  {name} refused")
        if name == "begin_director_rebind":
            return 11
        if name == "rebind_declared_pane_roles":
            revision, document, bindings, _why = values
            if revision != self.revision or document != self.document:
                raise RuntimeError("ERROR:  workflow changed since it was reviewed; reread before rebinding")
            if self.refuse_revert and bindings["director"]["runtime"] == "claude":
                raise RuntimeError("ERROR:  injected refusal of the undo")
            for role in self.document["roles"]:
                if role["name"] in bindings:
                    role.update(bindings[role["name"]])
            self.revision += 1
            if self.commit_then_raise and bindings["director"]["runtime"] == "codex":
                self.commit_then_raise = False
                raise RuntimeError("psql: server closed the connection unexpectedly")
            return self.revision
        if name == "finish_director_rebind":
            return {"rebind": 11, "state": values[1], "notice": 501 if values[1] == "applied" else None}
        if name == "director_rebind_unavailable":
            return {"rebind": 12, "told": True, "pause": {"pause": 3, "began": True}}
        return None

    def names(self) -> list[str]:
        return [name for name, _values in self.calls]


class Registering(RuntimeRunner):
    """A start that registers what the configuration named, as the board's row would."""

    def __init__(self, board: Board, *, refuse_kill: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.board = board
        self.refuse_kill = refuse_kill  # tmux refuses to stop a session: it stays live

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if self.refuse_kill and [str(part) for part in args][:2] == ["tmux", "kill-session"]:
            self.calls.append([str(part) for part in args])
            return subprocess.CompletedProcess(args, 1, stderr="error connecting to /tmp/tmux-1000/default")
        result = super().__call__(args, **kwargs)
        command = [str(part) for part in args]
        if command[:2] == ["tmux", "new-session"] and result.returncode == 0:
            self.board.registered = {"runtime": self.current_runtime, "actual_target": "porter-director:0.0",
                                     "process_pid": 4242, "process_start_time": 9, "process_uid": 1000,
                                     "generation": 4}
        if command[:2] == ["tmux", "kill-session"]:
            self.board.registered = None
        return result

    current_runtime = ""


def start(role: Any, *, config: Any, pane_state_dir: Path, runner: Any) -> int:
    runner.current_runtime = team_launcher._role_cli_name(role)
    return _fake_start(role, config=config, pane_state_dir=pane_state_dir, runner=runner)


def handover() -> runtime_handover.RuntimeHandover:
    return runtime_handover.RuntimeHandover(read_process=lambda _pid: None, sleep=lambda _s: None,
                                            settle_seconds=0.0, exit_timeout_seconds=0.0)


ELIGIBLE = {"codex": [], "agy": ["agy is not installed for porter"], "hermes": ["hermes is not installed for porter"],
            "copilot": ["copilot is not signed in for porter"]}


class Operator:
    def __init__(self, source: str = "pkexec", known: bool = True, name: str = "eric") -> None:
        self.source, self.known, self.name = source, known, name


class Journal:
    """rollout_journal.Attempt's surface, kept in memory."""

    opened: list["Journal"] = []

    def __init__(self, project: str, argv: list[str], operator: str = "") -> None:
        self.project, self.argv, self.operator_name, self.lines, self.closed = project, argv, operator, [], None
        Journal.opened.append(self)

    def open(self) -> Path:
        return Path("/nonexistent")

    def write(self, stream: str, text: str) -> None:
        self.lines.append((stream, text))

    def close(self, *, status: str, exit_status: int | None, detail: str = "") -> dict:
        self.closed = (status, exit_status, detail)
        return {}


class Case:
    def __init__(self, *, board: Board | None = None, eligible: dict | None = None, **runner: Any) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="syrd738-")
        self.root = Path(self.tmp.name)
        self.config_path = _write_config(self.root)
        self.config = team_launcher.load_project_config("porter", self.config_path)
        self.board = board or Board()
        self.runner = Registering(self.board, **runner)
        self.eligible = ELIGIBLE if eligible is None else eligible
        self.printed: list[str] = []
        sessions = self.root / "sessions"
        sessions.mkdir(exist_ok=True)
        role = dr._director(self.config)
        self.record = team_launcher.role_session_dir(self.config, role) / team_launcher.session_file_name(role.target)
        self.record.parent.mkdir(parents=True, exist_ok=True)
        self.record.write_text(json.dumps({"session_id": "old-claude-session", "cli": "claude"}))

    def close(self) -> None:
        self.tmp.cleanup()

    def run(self, *, cli: str = "codex", apply_it: bool = False, expect: str = "", euid: int = 0,
            operator: Operator | None = None, reason: str = REASON, hold: int = HOLD) -> int:
        from scripts.ticket_board import rollout_journal

        original = rollout_journal.Attempt
        rollout_journal.Attempt = Journal  # type: ignore[assignment]
        original_eligibility = dr.eligibility
        dr.eligibility = lambda *a, **k: copy.deepcopy(self.eligible)  # type: ignore[assignment]
        try:
            return dr.command(
                "porter", cli=cli, hold=hold, reason=reason, apply_it=apply_it, expect=expect,
                euid_getter=lambda: euid, operator_resolver=lambda: operator or Operator(),
                tenant_resolver=lambda slug, print_func=print: (Tenant(), self.config_path, self.config),
                board_get=self.board.get, admin=self.board, runner=self.runner, start=globals()["start"], handover=handover(),
                print_func=self.printed.append)
        finally:
            rollout_journal.Attempt = original  # type: ignore[assignment]
            dr.eligibility = original_eligibility  # type: ignore[assignment]

    def digest(self) -> str:
        line = next((l for l in reversed(self.printed) if "review           digest" in l), "")
        return line.rsplit(" ", 1)[-1]

    def configured(self) -> str:
        config = team_launcher.load_project_config("porter", self.config_path)
        return team_launcher._role_cli_name(dr._director(config))

    def text(self) -> str:
        return "\n".join(self.printed)


class Tenant:
    admin_database_url = "postgresql:///porter_ticket_board"


def attempt(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except (dr.RebindRefused, ValueError, KeyError, TypeError, RuntimeError) as exc:
        return f"{type(exc).__name__}: {exc}"


# -- tests ------------------------------------------------------------------------------------------------------------
def test_the_preview_says_what_would_happen_and_changes_nothing() -> None:
    case = Case()
    try:
        status = case.run()
        text = case.text()
        check(status == 0 and case.board.calls == [] and case.runner.sessions_killed() == []
              and case.runner.starts == [], f"a preview writes nothing and stops nothing: {status} {case.board.calls}")
        check(f"safety hold {HOLD} on claude" in text and "declared claude" in text and "pid 999999" in text
              and "director after   codex" in text and "codex: eligible" in text
              and "agy: agy is not installed" in text and "never started, typed into or resumed" in text,
              f"the preview names the hold, the Director now and after, and every alternate: {text}")
        check(case.board.packet["provenance"]["state_digest"] in text,
              "the preview names the packet's snapshot it would hand over")
        check(case.digest().startswith("sha256:") and f"--apply --expect {case.digest()}" in text,
              f"the preview ends with the exact command that applies it: {text[-300:]}")
        first = case.digest()
        case.run()
        check(case.digest() == first, "the same state previews to the same digest")
    finally:
        case.close()


def test_only_a_named_operator_through_pkexec_may_rebind() -> None:
    for kwargs, said in (({"euid": 1000}, "reads root's records"),
                         ({"operator": Operator(source="sudo")}, "elevated by sudo"),
                         ({"operator": Operator(known=False)}, "has to be authorized"),
                         ({"reason": "  "}, "needs a reason")):
        case = Case()
        try:
            status = case.run(apply_it=True, expect="sha256:x", **kwargs)
            check(status == 1 and case.board.calls == [] and case.runner.calls == [] and said in case.text(),
                  f"{kwargs}: refused before reading anything: {case.text()}")
        finally:
            case.close()


def test_a_hold_that_does_not_hold_the_director_is_refused() -> None:
    rows = rpt.raw(project="porter")
    rows["roles"] = [dict(r, runtime="codex") if r["name"] == "director" else r for r in rows["roles"]]
    case = Case(board=Board(built=rp.build(rows)))
    try:
        status = case.run(apply_it=True)
        check(status == 1 and "does not hold the Director" in case.text() and case.board.calls == [],
              f"a hold on another account is the Director's own to handle: {case.text()}")
    finally:
        case.close()


def test_a_target_that_is_held_or_not_ready_is_refused_before_anything_stops() -> None:
    for cli, said in (("claude", "the one this rebind moves the Director off"),
                      ("agy", "agy is not installed"), ("bogus", "not a runtime the launcher starts")):
        case = Case()
        try:
            case.run(cli=cli)
            digest = case.digest()
            status = case.run(cli=cli, apply_it=True, expect=digest)
            check(status == 1 and said in case.text() and case.board.calls == [] and case.runner.sessions_killed() == [],
                  f"{cli}: refused with nothing changed: {case.text()[-400:]}")
        finally:
            case.close()
    rows = rpt.raw(project="porter")
    rows["holds"] = rows["holds"] + [dict(rows["holds"][0], id=14, runtime="codex", role="main")]
    case = Case(board=Board(built=rp.build(rows, hold=HOLD)))
    try:
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        check(status == 1 and "codex account is under a safety hold" in case.text() and case.board.calls == [],
              f"an account under another hold is no refuge: {case.text()[-300:]}")
    finally:
        case.close()


def test_apply_needs_the_previewed_digest() -> None:
    case = Case()
    try:
        status = case.run(apply_it=True, expect="sha256:" + "0" * 64)
        check(status == 1 and "is not what this would do now" in case.text() and case.board.calls == [],
              f"a stale or invented digest applies nothing: {case.text()[-200:]}")
    finally:
        case.close()


def test_a_bad_packet_or_a_board_that_moves_is_refused() -> None:
    board = Board()
    board.packet = dict(board.packet, provenance=dict(board.packet["provenance"], project="other"))
    case = Case(board=board)
    try:
        status = case.run()
        check(status == 1 and "does not validate" in case.text() and case.board.calls == [],
              f"a packet that does not validate is not handed to anyone: {case.text()}")
    finally:
        case.close()
    board = Board()
    first = board.packet
    later = packet(generated_at=rpt.at(5), notification_total=4,
                   notifications=rpt.raw()["notifications"] + [dict(rpt.raw()["notifications"][0], id=13)])
    reads = iter([first, later])
    board.get = lambda path, _orig=board.get: next(reads) if path.startswith(rp.PATH) else _orig(path)  # type: ignore
    case = Case(board=board)
    try:
        status = case.run()
        check(status == 1 and "changed between two reads" in case.text(),
              f"a board that moves between the packet's two reads is read again: {case.text()}")
    finally:
        case.close()


def test_no_eligible_alternate_keeps_work_paused_and_tells_the_operator_once() -> None:
    none = {runtime: ["not installed"] for runtime in ELIGIBLE}
    case = Case(eligible=none)
    try:
        status = case.run()
        check(status == 1 and "no runtime is eligible" in case.text() and case.board.calls == [],
              f"the preview says so and changes nothing: {case.text()[-300:]}")
        status = case.run(apply_it=True, expect="sha256:" + "1" * 64)
        check(status == 1 and case.board.calls == [] and "is not what this would do now" in case.text(),
              f"even pausing work takes the previewed digest: {case.text()[-200:]}")
        status = case.run(apply_it=True, expect=case.digest())
        check(status == 1 and case.board.names() == ["director_rebind_unavailable"]
              and case.runner.sessions_killed() == [] and case.runner.starts == [],
              f"--apply pauses and tells, and touches no session: {case.board.calls}")
        hold, operator, reason, detail = case.board.calls[0][1]
        check(hold == HOLD and operator == "eric" and reason == REASON and detail == none
              and "Work is paused (drain, pause 3)" in case.text() and "is told" in case.text(),
              f"recorded with the operator, the reason and what each runtime lacks: {case.board.calls[0]}")
    finally:
        case.close()


def test_apply_replaces_the_director_and_hands_it_the_validated_packet() -> None:
    case = Case()
    try:
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        names = case.board.names()
        check(status == 0, f"the rebind succeeds: {case.text()[-600:]}")
        check(names[0] == "begin_director_rebind" and names[-1] == "finish_director_rebind"
              and "rebind_declared_pane_roles" in names,
              f"the board records the rebind first and finishes it last: {names}")
        begin = case.board.calls[0][1]
        check(begin[0] == HOLD and begin[1] == "eric" and begin[2] == REASON and begin[3] == "codex"
              and begin[5]["registration"] == {k: v for k, v in Board().registered.items() if k != "worktree"}
              and begin[5]["declared"] == {"runtime": "claude", "target": "porter-director:0.0", "slot": 0}
              and begin[5]["session_record"]["session_id"] == "old-claude-session"
              and begin[6]["state_digest"] == case.board.packet["provenance"]["state_digest"]
              and begin[6]["packet_digest"] == case.board.packet["packet_digest"] and begin[6]["hold"] == HOLD,
              f"the old identity, the evidence session and the packet's provenance are journalled: {begin}")
        steps = [values[1] for name, values in case.board.calls if name == "note_director_rebind_step"]
        check(steps == ["old_stopped", "session_set_aside", "workflow", "projection", "packet_written", "started"],
              f"each step is recorded as it is made: {steps}")
        check(case.board.declared() == "codex" and case.configured() == "codex"
              and case.board.registered["runtime"] == "codex",
              f"the board declares, the launcher configures and the board registers codex: "
              f"{case.board.declared()} {case.configured()} {case.board.registered}")
        check(case.runner.sessions_killed() == ["porter-director"] and case.runner.starts == [("porter-director", "codex")],
              f"the old session is stopped once and only the new runtime is started: {case.runner.starts}")
        check(not any("send-keys" in c or "paste-buffer" in c for call in case.runner.calls for c in call),
              "nothing is typed into any pane")
        finish = next(values for name, values in case.board.calls if name == "finish_director_rebind")
        written = Path(finish[3])
        check(finish[1] == "applied" and finish[2]["registration"]["runtime"] == "codex" and written.is_file()
              and json.loads(written.read_text()) == case.board.packet
              and rp.validate(json.loads(written.read_text())) == case.board.packet
              and oct(written.stat().st_mode & 0o777) == "0o600",
              f"the new session's packet is the validated one, private to it: {finish}")
        check(not case.record.exists(), "the old session record is set aside, so the new session starts fresh")
        evidence = case.record.with_name(f"held-session-hold-{HOLD}.json")
        kept = [p.name for p in case.record.parent.iterdir() if ".kept." in p.name or p.name.endswith(".claude")]
        set_aside = next(values[2] for name, values in case.board.calls
                         if name == "note_director_rebind_step" and values[1] == "session_set_aside")
        check(evidence.is_file() and json.loads(evidence.read_text())["session_id"] == "old-claude-session"
              and oct(evidence.stat().st_mode & 0o777) == "0o600" and set_aside["evidence"] == str(evidence)
              and kept == [],
              f"the held session is kept as evidence, never as a record a switch back would resume: "
              f"{sorted(p.name for p in case.record.parent.iterdir())}")
        check(f"hold {HOLD} stands until it is cleared" in case.text() and "clear" not in " ".join(case.board.names()),
              f"the hold is not cleared: {case.board.names()}")
        journal = Journal.opened[-1]
        check(journal.closed and journal.closed[0] == "succeeded" and journal.operator_name == "eric"
              and json.loads(journal.lines[0][1])["plan"]["review"] == case.digest(),
              f"root's rollout journal holds the reviewed plan and the outcome: {journal.closed}")
    finally:
        case.close()


def test_a_replacement_that_fails_to_start_is_undone_without_starting_the_held_account() -> None:
    case = Case(fail_start=True)
    try:
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        check(status == 1, f"the rebind fails: {case.text()[-400:]}")
        check(case.runner.starts == [("porter-director", "codex")]
              and not any(runtime == "claude" for _s, runtime in case.runner.starts),
              f"the held runtime is never started, not even to roll back: {case.runner.starts}")
        check(case.board.declared() == "claude" and case.configured() == "claude"
              and "porter-director" not in case.runner.live and case.board.registered is None,
              f"one authoritative identity: declared and configured claude, and no live Director: "
              f"{case.board.declared()} {case.configured()} {case.runner.live} {case.board.registered}")
        finish = next(values for name, values in case.board.calls if name == "finish_director_rebind")
        check(finish[1] == "rolled_back" and finish[2]["director_started"] is False
              and finish[2]["undone"] == ["new_session", "workflow", "projection"] and finish[2]["problems"] == [],
              f"the board records the rollback and its result: {finish}")
        check("left stopped" in case.text() and "Work is paused (drain)" in case.text(),
              f"the operator is told what stands: {case.text()[-300:]}")
    finally:
        case.close()


def test_a_replacement_the_board_registers_wrongly_is_stopped_and_undone() -> None:
    case = Case()
    try:
        case.run()

        real_start = globals()["start"]

        def wrong(role: Any, *, config: Any, pane_state_dir: Path, runner: Any) -> int:
            status = real_start(role, config=config, pane_state_dir=pane_state_dir, runner=runner)
            case.board.registered = dict(case.board.registered, runtime="agy")  # some other process took the role
            return status
        globals()["start"] = wrong
        try:
            status = case.run(apply_it=True, expect=case.digest())
        finally:
            globals()["start"] = real_start
        finish = next(values for name, values in case.board.calls if name == "finish_director_rebind")
        check(status == 1 and finish[1] == "rolled_back" and "registers the Director as agy" in finish[2]["failure"]
              and finish[2]["undone"] == ["new_session", "workflow", "projection"]
              and case.runner.sessions_killed() == ["porter-director", "porter-director"]
              and "porter-director" not in case.runner.live,
              f"a started replacement the board does not register as codex is stopped before the undo: {finish} "
              f"{case.runner.sessions_killed()}")
    finally:
        case.close()


def test_a_board_that_refuses_the_rebind_leaves_the_director_stopped_on_its_old_runtime() -> None:
    case = Case()
    try:
        case.run()
        digest = case.digest()
        case.board.revision += 1  # another writer moved the workflow after the preview
        case.board.packet = case.board.packet  # the packet is unchanged
        status = case.run(apply_it=True, expect=digest)
        check(status == 1 and "is not what this would do now" in case.text(),
              f"a moved workflow changes the digest, so nothing is applied: {case.text()[-200:]}")
        case.board.calls.clear()
        case.run()
        board = case.board
        real_call = board.call

        def racing(function: str, *args: Any) -> Any:
            if function.endswith("rebind_declared_pane_roles") and board.declared() == "claude":
                board.revision += 1  # moved between the preview and the write
            return real_call(function, *args)
        board.call = racing  # type: ignore[assignment]
        status = case.run(apply_it=True, expect=case.digest())
        finish = next(values for name, values in board.calls if name == "finish_director_rebind")
        check(status == 1 and finish[1] == "rolled_back" and finish[2]["undone"] == ["workflow"]
              and board.declared() == "claude" and case.configured() == "claude"
              and case.runner.starts == [] and "workflow changed since it was reviewed" in finish[2]["failure"],
              f"the compare-and-swap refuses, and the Director is left stopped on claude: {finish}")
    finally:
        case.close()


def test_a_rebind_that_commits_without_answering_is_still_undone() -> None:
    for board, said in ((Board(fail_reads_after_rebind=1), "the board socket timed out"),
                        (Board(commit_then_raise=True), "server closed the connection")):
        case = Case(board=board)
        try:
            case.run()
            status = case.run(apply_it=True, expect=case.digest())
            finish = next(values for name, values in case.board.calls if name == "finish_director_rebind")
            check(status == 1 and finish[1] == "rolled_back" and said in finish[2]["failure"]
                  and "workflow" in finish[2]["undone"] and case.board.declared() == "claude"
                  and case.configured() == "claude" and case.runner.starts == [],
                  f"{said}: the committed rebind is put back, and only then called rolled back: {finish} "
                  f"declared={case.board.declared()} configured={case.configured()}")
        finally:
            case.close()


def test_a_projection_that_writes_and_then_fails_is_still_undone() -> None:
    from scripts import runtime_projection

    real = runtime_projection.write_runtime_projection

    def writes_then_fails(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        raise SystemExit("switchyard: chown of the projection failed")
    case = Case()
    try:
        case.run()
        runtime_projection.write_runtime_projection = writes_then_fails  # type: ignore[assignment]
        try:
            status = case.run(apply_it=True, expect=case.digest())
        finally:
            runtime_projection.write_runtime_projection = real  # type: ignore[assignment]
        finish = next(values for name, values in case.board.calls if name == "finish_director_rebind")
        check(status == 1 and finish[1] == "rolled_back" and finish[2]["undone"] == ["workflow", "projection"]
              and case.board.declared() == "claude" and case.configured() == "claude",
              f"a written projection is put back even when its write did not return: {finish} "
              f"declared={case.board.declared()} configured={case.configured()}")
    finally:
        case.close()


def test_an_undo_is_called_rolled_back_only_when_the_end_state_says_so() -> None:
    from scripts import runtime_projection

    real = runtime_projection.restore_projection
    case = Case(fail_start=True)
    try:
        case.run()
        runtime_projection.restore_projection = lambda journal: None  # type: ignore[assignment]
        try:
            status = case.run(apply_it=True, expect=case.digest())
        finally:
            runtime_projection.restore_projection = real  # type: ignore[assignment]
        finish = next(values for name, values in case.board.calls if name == "finish_director_rebind")
        check(status == 1 and finish[1] == "needs_operator"
              and any("the configuration names codex" in p for p in finish[2]["problems"])
              and "needs an operator" in case.text() and "declared and configured as claude again" not in case.text(),
              f"an undo whose result does not read back as the held runtime is not called clean: {finish}")
    finally:
        case.close()


def test_a_held_director_that_will_not_stop_is_never_called_stopped() -> None:
    case = Case(refuse_kill=True)
    try:
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        finish = next(values for name, values in case.board.calls if name == "finish_director_rebind")
        check(status == 1 and finish[1] == "needs_operator"
              and any("a Director session is live" in problem for problem in finish[2]["problems"])
              and "porter-director" in case.runner.live and case.runner.starts == []
              and case.board.declared() == "claude" and case.configured() == "claude"
              and "left stopped" not in case.text() and "needs an operator" in case.text(),
              f"a held Director that tmux would not stop is reported as still running, never as left stopped: "
              f"{finish} {case.text()[-300:]}")
    finally:
        case.close()


def test_an_undo_that_cannot_finish_needs_an_operator() -> None:
    case = Case(board=Board(refuse_revert=True), fail_start=True)
    try:
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        finish = next(values for name, values in case.board.calls if name == "finish_director_rebind")
        check(status == 1 and finish[1] == "needs_operator" and finish[2]["undone"] == ["new_session"]
              and any("injected refusal" in p for p in finish[2]["problems"]),
              f"an undo the board refuses is recorded as needing an operator: {finish}")
        check(case.board.declared() == "codex" and case.configured() == "codex" and case.runner.starts[-1][1] == "codex"
              and "needs an operator" in case.text() and "was not started on claude" in case.text(),
              f"the projection is not put back against a board that still declares codex: {case.text()[-300:]}")
    finally:
        case.close()


def test_a_board_that_refuses_to_begin_changes_nothing() -> None:
    case = Case(board=Board(refuse="begin_director_rebind"))
    try:
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        check(status == 1 and "refused the rebind" in case.text() and case.runner.sessions_killed() == []
              and case.board.declared() == "claude" and case.configured() == "claude",
              f"nothing stops when the board will not record the rebind: {case.text()[-200:]}")
    finally:
        case.close()


def test_the_admin_call_passes_values_as_psql_variables() -> None:
    seen: list[tuple[list[str], str]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0, stdout='{"rebind": 3}\n', stderr="")

    admin = dr.PsqlAdmin("postgresql:///porter_ticket_board", runner=runner)
    said = admin.call("ticket_board.finish_director_rebind", (3, "bigint"), ("applied", "text"),
                      ({"a": "b'; DROP TABLE x; --"}, "jsonb"), (None, "text"))
    argv, sql = seen[0]
    check(said == {"rebind": 3} and argv[:4] == ["sudo", "-u", "postgres", "psql"]
          and "a2={\"a\": \"b'; DROP TABLE x; --\"}" in argv and "a3=" in argv
          and sql == "SELECT ticket_board.finish_director_rebind(NULLIF(:'a0', '')::bigint, NULLIF(:'a1', '')::text, "
                     "NULLIF(:'a2', '')::jsonb, NULLIF(:'a3', '')::text);\n",
          f"values travel as psql variables, quoted by psql, never spliced into SQL: {argv} {sql}")

    def failing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 3, stdout="", stderr="psql:<stdin>:1: ERROR:  hold 9 is not current\n")

    said = attempt(dr.PsqlAdmin("x", runner=failing).call, "ticket_board.begin_director_rebind", (9, "bigint"))
    check(said == "RuntimeError: psql:<stdin>:1: ERROR:  hold 9 is not current",
          f"the database's refusal is the error: {said}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"director_rebind_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
