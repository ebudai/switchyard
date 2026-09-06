#!/usr/bin/env python3
"""Regression coverage for changing an existing role's agent runtime."""

from __future__ import annotations

import getpass
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import presentation_controller, role_runtime, team_launcher  # noqa: E402

DIRECTOR_ENV = {"TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_PROJECT": "porter"}


def _workflow_document(project: str = "porter") -> dict[str, Any]:
    return {
        "schema": "switchyard.workflow.v1",
        "project": project,
        "roles": [
            {"name": "director", "kind": "system", "active": True, "label": "Director",
             "runtime": "claude", "target": f"{project}-director:0.0", "slot": 0, "capabilities": []},
            {"name": "audit", "kind": "auditor", "active": True, "label": "Audit",
             "runtime": "claude", "target": f"{project}-audit:0.0", "slot": 1, "capabilities": []},
            {"name": "research", "kind": "implementer", "active": True, "label": "Research",
             "runtime": "codex", "target": f"{project}-research:0.0", "slot": None, "capabilities": []},
            {"name": "user", "kind": "system", "active": True, "label": "User",
             "runtime": None, "target": None, "slot": None, "capabilities": []},
        ],
        "stages": [],
        "transitions": [],
        "flags": {},
        "remove_stages": [],
    }


class FakeBoard:
    """The board's workflow apply, with its optimistic-concurrency behaviour."""

    def __init__(self, document: dict[str, Any] | None = None, *, fail_apply: str = "") -> None:
        self.document = document or _workflow_document()
        self.revision = 7
        self.applies: list[tuple[dict[str, Any], bool]] = []
        self.fail_apply = fail_apply

    def configure_workflow(self, document: dict[str, Any], *, expected_revision: int, dry_run: bool = False):
        self.applies.append((json.loads(json.dumps(document)), dry_run))
        if self.fail_apply and not dry_run:
            raise RuntimeError(self.fail_apply)
        if not dry_run:
            if expected_revision and expected_revision != self.revision:
                raise RuntimeError(f"workflow revision moved: expected {expected_revision}, have {self.revision}")
            self.document = json.loads(json.dumps(document))
            self.revision += 1
        return {"revision": self.revision}

    def runtime_of(self, role: str) -> str | None:
        return next(entry["runtime"] for entry in self.document["roles"] if entry["name"] == role)

    def reader(self, **_kwargs: Any) -> tuple[dict[str, Any], int]:
        return json.loads(json.dumps(self.document)), self.revision


def _write_config(root: Path, *, project: str = "porter") -> Path:
    provision = root / ".switchyard" / "provision"
    provision.mkdir(parents=True)
    layout = provision / f"{project}-konsole-layout.json"
    layout.write_text(
        json.dumps(team_launcher._new_project_layout_payload(2), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    roles = []
    for slot, (name, cli, detached) in enumerate(
        (("director", "claude", False), ("audit", "claude", False), ("research", "codex", True))
    ):
        workdir = root / name
        workdir.mkdir()
        roles.append(
            {
                "role": name,
                "slot": None if detached else slot,
                "detached": detached,
                "target": f"{project}-{name}:0.0",
                "tmux_session": f"{project}-{name}",
                "workdir": str(workdir),
                "cli": [cli],
                "live_commands": [cli],
            }
        )
    config_path = provision / f"{project}.json"
    config_path.write_text(
        json.dumps(
            {
                "project": project,
                "layout": str(layout),
                "session_dir": str(root / "sessions"),
                "board_url": "http://127.0.0.1:65535",
                "presentation": {"slot_count": 2},
                "roles": roles,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


class RuntimeRunner:
    """tmux and process calls, recorded; sessions exist unless killed."""

    def __init__(self, *, live: set[str] | None = None, fail_start: bool = False) -> None:
        self.live = live if live is not None else {"porter-director", "porter-audit", "porter-research"}
        self.calls: list[list[str]] = []
        self.fail_start = fail_start

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = [str(part) for part in args]
        self.calls.append(command)
        if command[:2] == ["tmux", "has-session"]:
            session = command[command.index("-t") + 1].removeprefix("=").split(":", 1)[0]
            return subprocess.CompletedProcess(command, 0 if session in self.live else 1)
        if command[:2] == ["tmux", "kill-session"]:
            self.live.discard(command[command.index("-t") + 1].removeprefix("=").split(":", 1)[0])
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["tmux", "new-session"]:
            if self.fail_start:
                return subprocess.CompletedProcess(command, 3, stderr="injected start failure")
            self.live.add(command[command.index("-s") + 1])
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["tmux", "display-message"]:
            return subprocess.CompletedProcess(command, 0, stdout="0\n")
        return subprocess.CompletedProcess(command, 0)

    def sessions_started(self) -> list[str]:
        return [c[c.index("-s") + 1] for c in self.calls if c[:2] == ["tmux", "new-session"] and "-s" in c]

    def sessions_killed(self) -> list[str]:
        return [
            c[c.index("-t") + 1].removeprefix("=").split(":", 1)[0]
            for c in self.calls
            if c[:2] == ["tmux", "kill-session"]
        ]


def _ready(*_args: Any, **_kwargs: Any) -> tuple[str, ...]:
    return ()


def _idle_state(config_path: Path, *targets: str) -> Path:
    state_dir = config_path.parent / "pane-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _fake_start(role: Any, *, config: Any, pane_state_dir: Path, runner: Any) -> int:
    """Stand in for the launcher's start path, which has its own suites."""
    return runner(["tmux", "new-session", "-d", "-s", role.tmux_session]).returncode


def _switch(config_path: Path, *, role: str, runtime: str, board: FakeBoard, runner: RuntimeRunner,
            environ: dict[str, str] | None = None, **kwargs: Any):
    config = team_launcher.load_project_config("porter", config_path)
    return role_runtime.switch_role_runtime(
        config,
        config_path=config_path,
        role_name=role,
        runtime=runtime,
        environ=environ if environ is not None else DIRECTOR_ENV,
        runner=runner,
        client=board,
        workflow_reader=board.reader,
        pane_state_dir=config_path.parent / "pane-state",
        print_func=lambda _line: None,
        start=kwargs.pop("start", _fake_start),
        busy_check=kwargs.pop("busy_check", lambda *a, **k: False),
        **kwargs,
    )


def test_a_visible_idle_role_switches_provider_and_its_slot_is_reconnected() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path, "porter-audit:0.0")
        board = FakeBoard()
        runner = RuntimeRunner()
        role_runtime._readiness_blockers = _ready

        result = _switch(config_path, role="audit", runtime="agy", board=board, runner=runner)

        assert result.configured_changed
        assert (result.previous_runtime, result.runtime) == ("claude", "agy")
        # The live session really was replaced, and the slot showing it was put back.
        assert result.live_session_changed
        assert "porter-audit" in runner.sessions_killed()
        assert "porter-audit" in runner.sessions_started()
        assert result.reconnected_slots == (1,)
        # Both records agree afterwards.
        assert board.runtime_of("audit") == "agy"
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        assert next(r["cli"] for r in raw["roles"] if r["role"] == "audit") == ["agy"]
        # And the journal is gone, because there is nothing left to undo.
        assert not role_runtime.journal_path_for(
            team_launcher.load_project_config("porter", config_path),
            config_path=config_path,
            role_name="audit",
        ).exists()


def test_the_slot_is_reconnected_only_after_the_replacement_session_is_live() -> None:
    """The defect this ticket exists for.

    The old worker's close parks the proxy on a recovery message. Reconnecting
    before the replacement is up attaches it to nothing and parks it again, so
    the ordering is the fix, not an implementation detail.
    """
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-order.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path, "porter-audit:0.0")
        board = FakeBoard()
        runner = RuntimeRunner()
        role_runtime._readiness_blockers = _ready

        _switch(config_path, role="audit", runtime="agy", board=board, runner=runner)

        commands = [" ".join(call) for call in runner.calls]
        killed = next(i for i, c in enumerate(commands) if c.startswith("tmux kill-session") and "porter-audit" in c)
        started = next(
            i for i, c in enumerate(commands)
            if c.startswith("tmux new-session") and "-s porter-audit" in c
        )
        respawned = [i for i, c in enumerate(commands) if "respawn-pane" in c]
        assert killed < started, "the old session must go before the new one starts"
        assert respawned, "the slot was never reconnected"
        assert min(respawned) > started, "a slot was reconnected before the worker was live"


def test_a_busy_role_is_refused_unless_the_interruption_is_narrated() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-busy.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path)
        busy = lambda *a, **k: True  # noqa: E731 - the gate itself is covered by its own suite
        board = FakeBoard()
        runner = RuntimeRunner()
        role_runtime._readiness_blockers = _ready

        try:
            _switch(config_path, role="audit", runtime="agy", board=board, runner=runner, busy_check=busy)
            raise AssertionError("a busy role was switched without --force")
        except role_runtime.RoleRuntimeRefusal as refusal:
            assert "busy" in str(refusal) and "--force" in str(refusal)
        # Nothing moved.
        assert board.runtime_of("audit") == "claude"
        assert runner.sessions_killed() == []

        # --force alone is not enough; it has to say why.
        try:
            _switch(config_path, role="audit", runtime="agy", board=board, runner=runner, force=True, busy_check=busy)
            raise AssertionError("--force was accepted without a reason")
        except role_runtime.RoleRuntimeRefusal as refusal:
            assert "--reason" in str(refusal)

        forced = _switch(
            config_path, role="audit", runtime="agy", board=board, runner=runner,
            force=True, reason="provider outage during a release", busy_check=busy,
        )
        assert forced.forced and forced.reason == "provider outage during a release"
        assert board.runtime_of("audit") == "agy"


def test_a_runtime_that_cannot_start_is_refused_before_anything_stops() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-ready.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path, "porter-audit:0.0")
        board = FakeBoard()
        runner = RuntimeRunner()
        role_runtime._readiness_blockers = lambda *a, **k: ("agy is not logged in for porter-agent",)

        try:
            _switch(config_path, role="audit", runtime="agy", board=board, runner=runner)
            raise AssertionError("an unusable runtime was accepted")
        except role_runtime.RoleRuntimeRefusal as refusal:
            assert "not logged in" in str(refusal)
        assert board.runtime_of("audit") == "claude"
        assert runner.sessions_killed() == [], "the running role was stopped before the new CLI was checked"
        assert board.applies == [], "the board was written to before the preflight passed"


def test_a_new_runtime_that_fails_to_start_is_rolled_back_to_the_old_one() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-rollback.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path)
        before = config_path.read_text(encoding="utf-8")
        board = FakeBoard()
        runner = RuntimeRunner(fail_start=True)
        role_runtime._readiness_blockers = _ready

        try:
            _switch(config_path, role="audit", runtime="agy", board=board, runner=runner)
            raise AssertionError("a failed start reported success")
        except role_runtime.RoleRuntimeRefusal as refusal:
            assert "still runs claude" in str(refusal), refusal
            assert "undone" in str(refusal)

        # Both records are back where they started, and they agree.
        assert board.runtime_of("audit") == "claude"
        assert config_path.read_text(encoding="utf-8") == before
        # Nothing is left for an operator to clean up.
        assert not role_runtime.journal_path_for(
            team_launcher.load_project_config("porter", config_path),
            config_path=config_path,
            role_name="audit",
        ).exists()


def test_a_rollback_that_cannot_finish_is_reported_with_the_journal_that_repairs_it() -> None:
    """A wrong state an operator knows about is recoverable; a silent one is not."""
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-stuck.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path)
        board = FakeBoard()
        runner = RuntimeRunner(fail_start=True)
        role_runtime._readiness_blockers = _ready

        applied: list[bool] = []

        def refuse_second_apply(document, *, expected_revision, dry_run=False):
            if not dry_run:
                applied.append(True)
                if len(applied) > 1:
                    raise RuntimeError("board rejected the restoring document")
            return board.configure_workflow(document, expected_revision=expected_revision, dry_run=dry_run)

        board_proxy = type("Proxy", (), {"configure_workflow": staticmethod(refuse_second_apply)})()
        try:
            config = team_launcher.load_project_config("porter", config_path)
            role_runtime.switch_role_runtime(
                config,
                config_path=config_path,
                role_name="audit",
                runtime="agy",
                environ=DIRECTOR_ENV,
                runner=runner,
                client=board_proxy,
                workflow_reader=board.reader,
                pane_state_dir=config_path.parent / "pane-state",
                start=_fake_start,
                busy_check=lambda *a, **k: False,
                print_func=lambda _line: None,
            )
            raise AssertionError("a stuck rollback reported success")
        except role_runtime.RoleRuntimeRefusal as refusal:
            assert "needs an operator" in str(refusal)
            assert "workflow" in str(refusal)
            journal = role_runtime.journal_path_for(
                team_launcher.load_project_config("porter", config_path),
                config_path=config_path,
                role_name="audit",
            )
            assert str(journal) in str(refusal)
            assert journal.exists(), "the journal that repairs it was deleted"
            recorded = json.loads(journal.read_text(encoding="utf-8"))
            assert recorded["previous_runtime"] == "claude"
            assert recorded["previous_workflow_revision"] == 7
            assert recorded["steps_applied"]


def test_a_detached_role_switches_without_a_slot_to_reconnect() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-detached.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path)
        board = FakeBoard()
        runner = RuntimeRunner()
        role_runtime._readiness_blockers = _ready

        result = _switch(config_path, role="research", runtime="hermes", board=board, runner=runner)

        assert result.configured_changed and result.runtime == "hermes"
        assert result.reconnected_slots == (), "a detached role has no slot to reconnect"
        assert "no display slot showed it" in result.describe()
        assert board.runtime_of("research") == "hermes"
        # The visible roles were left entirely alone.
        assert runner.sessions_killed() == ["porter-research"]


def test_a_role_the_board_does_not_run_is_refused_rather_than_half_changed() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-noruntime.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        board = FakeBoard()
        document, _revision = board.reader()
        try:
            role_runtime.document_with_runtime(document, role="user", runtime="codex")
            raise AssertionError("a role with no runtime was given one")
        except role_runtime.RoleRuntimeRefusal as refusal:
            assert "no runtime" in str(refusal)
        try:
            role_runtime.document_with_runtime(document, role="nobody", runtime="codex")
            raise AssertionError("an unknown role was accepted")
        except role_runtime.RoleRuntimeRefusal as refusal:
            assert "not in the declared workflow" in str(refusal)


def test_the_workflow_and_the_launcher_projection_stay_consistent() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-consistency.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path)
        board = FakeBoard()
        runner = RuntimeRunner()
        role_runtime._readiness_blockers = _ready

        _switch(config_path, role="audit", runtime="codex", board=board, runner=runner)

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        audit = next(role for role in raw["roles"] if role["role"] == "audit")
        assert audit["cli"] == ["codex"]
        assert board.runtime_of("audit") == "codex"
        # Resume semantics belong to the runtime: codex resumes by subcommand,
        # so a claude --resume flag left behind would be handed to the wrong CLI.
        assert audit["resume_mode"] == "subcommand"
        assert audit["resume_subcommand"] == "resume"
        assert "resume_flag" not in audit
        # Every other role is untouched.
        assert [role["cli"] for role in raw["roles"] if role["role"] != "audit"] == [["claude"], ["codex"]]
        # The board saw a validated dry run before the real apply.
        assert [dry for _document, dry in board.applies] == [True, False]


def test_a_later_upgrade_keeps_the_selected_runtime() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-upgrade.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path)
        board = FakeBoard()
        runner = RuntimeRunner()
        role_runtime._readiness_blockers = _ready
        _switch(config_path, role="audit", runtime="agy", board=board, runner=runner)

        upgraded = team_launcher.upgrade_generated_project_layout(
            team_launcher.load_project_config("porter", config_path),
            config_path=config_path,
            dry_run=False,
            runner=RuntimeRunner(),
        )
        assert upgraded is not None
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        assert next(role["cli"] for role in raw["roles"] if role["role"] == "audit") == ["agy"]
        assert team_launcher._role_cli_name(
            team_launcher._role_by_name(team_launcher.load_project_config("porter", config_path), "audit")
        ) == "agy"


def test_the_command_cannot_act_for_another_role_or_another_tenant() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime-authz.") as tmp:
        root = Path(tmp)
        config_path = _write_config(root)
        _idle_state(config_path)
        board = FakeBoard()
        runner = RuntimeRunner()
        role_runtime._readiness_blockers = _ready

        for environ, expected in (
            ({"TICKET_BOARD_CALLER_ROLE": "app"}, "requires TICKET_BOARD_CALLER_ROLE=director"),
            ({}, "requires TICKET_BOARD_CALLER_ROLE=director"),
            (
                {"TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_PROJECT": "otherproject"},
                "cross-project",
            ),
        ):
            try:
                _switch(config_path, role="audit", runtime="agy", board=board, runner=runner, environ=environ)
                raise AssertionError(f"accepted {environ}")
            except role_runtime.RoleRuntimeRefusal as refusal:
                assert expected in str(refusal), (environ, str(refusal))
        # Refused before anything was read from or written to the board.
        assert board.applies == []
        assert runner.sessions_killed() == []
        assert board.runtime_of("audit") == "claude"


def test_the_public_command_is_a_director_command_that_needs_no_root() -> None:
    assert "set-role-runtime" in team_launcher.SWITCHYARD_COMMANDS
    assert "set-role-runtime" not in team_launcher.SWITCHYARD_PRIVILEGED_COMMANDS
    assert not team_launcher.switchyard_invocation_requires_root(
        ["set-role-runtime", "porter", "audit", "--cli", "agy"]
    )
    parser = team_launcher._build_switchyard_set_role_runtime_parser()
    args = parser.parse_args(["porter", "audit", "--cli", "agy", "--force", "--reason", "why"])
    assert (args.project, args.role, args.cli, args.force, args.reason) == (
        "porter", "audit", "agy", True, "why"
    )
    # An unsupported runtime cannot even be spelled.
    try:
        parser.parse_args(["porter", "audit", "--cli", "notacli"])
        raise AssertionError("an unsupported runtime was accepted")
    except SystemExit:
        pass


if __name__ == "__main__":
    run_module_tests(globals())
    print("role_runtime_test: ok")
