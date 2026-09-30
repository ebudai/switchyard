#!/usr/bin/env python3
"""A runtime switch counts the board's declaration, so the launcher's runtime can be restored.

MEFP, 2026-09-30 (SYRD-525): `switchyard set-role-runtime mefp luna-6 --cli
claude --model claude-sonnet-5-5` ran on a release older than SYRD-486. Its
start check still expected Codex, so the Claude session it started read as
absent; its rollback restored the launcher config first and then undid the
workflow with expected_revision=0, which the board refuses. luna-6 was left
with the board declaring claude, the launcher config naming codex, and no
session.

From that split, the command an operator reaches for to go back --
`set-role-runtime mefp luna-6 --cli codex` -- compared codex with the launcher
config alone, found it equal, and answered "already runs codex; nothing to
change" with exit 0. The board still declared claude, so luna-6 could not be
brought up under codex against it. These cases build that state directly --
board, config, a leftover journal from the failed switch, and no session --
and run the real switch over the SYRD-486 fixtures: tmux stood in for, every
session a real process named after the CLI the config names, and a board with
the real SQL's revision rule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import role_runtime_consistency_test as c  # noqa: E402
import role_runtime_test as base  # noqa: E402

CHECKS = 0
#: What the release MEFP runs left behind: its own journal of the failed switch.
LEFTOVER_JOURNAL = {
    "project": "porter", "role": "audit", "previous_runtime": "codex", "requested_runtime": "claude",
    "previous_workflow_revision": 7, "steps_applied": ["workflow", "projection", "worker_stopped"],
}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _split(tmp: Path, *, board_runtime: str = "claude", live: tuple[str, ...] = (), journal: bool = True):
    """MEFP's luna-6 after the failed canary: the board on claude, the launcher on codex."""
    config_path = c._tenant(tmp, cli="codex", live_commands=["codex"])
    document = base._workflow_document()
    for role in document["roles"]:
        if role["name"] == "audit":
            role["runtime"] = board_runtime
    board = c.StrictBoard(document)
    board.revision = 8
    host = c.Host(tmp, config_path, live=live)
    if journal:
        path = c._journal(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(LEFTOVER_JOURNAL), encoding="utf-8")
    return config_path, board, host


@c._sandbox
def test_asking_for_the_launchers_runtime_moves_the_board_back(tmp: Path) -> None:
    config_path, board, host = _split(tmp)
    before = config_path.read_bytes()
    try:
        result, refused, said = c._switch(config_path, host, board, "codex")
        check(result is not None, (refused, said))
        check(result.configured_changed, f"the split was answered as nothing to change: {said}")
        check(board.runtime_of("audit") == "codex" and board.revision == 9, (board.document, board.revision))
        # The launcher already named codex; its projection is the same runtime.
        entry = c._entry(config_path)
        check(entry["cli"] == ["codex"] and entry["live_commands"] == ["codex"], entry)
        # Nothing else in it moves: other roles are untouched, and this one gains
        # at most the runtime's own resume semantics, which any switch to it writes.
        old = {role["role"]: role for role in json.loads(before)["roles"]}
        new = {role["role"]: role for role in json.loads(config_path.read_bytes())["roles"]}
        check({name: role for name, role in new.items() if name != "audit"}
              == {name: role for name, role in old.items() if name != "audit"}, "another role changed")
        moved = {key for key in set(old["audit"]) | set(new["audit"]) if old["audit"].get(key) != new["audit"].get(key)}
        check(moved <= {"resume_mode", "resume_subcommand", "resume_flag"}, f"audit changed beyond resume: {moved}")
        # It was not running, so nothing is started behind the operator's back.
        check(not [call for call in host.calls if call[:2] == ["tmux", "new-session"]], host.calls)
        check("was codex, while the board declared claude" in result.describe(), result.describe())
        check(not c._journal(config_path).exists(), "a finished switch keeps no journal")
    finally:
        host.close()


@c._sandbox
def test_the_canary_can_be_completed_from_the_split_instead(tmp: Path) -> None:
    config_path, board, host = _split(tmp)
    try:
        result, refused, said = c._switch(config_path, host, board, "claude", model="claude-sonnet-5-5")
        check(result is not None and result.configured_changed, (refused, said))
        entry = c._entry(config_path)
        check(entry["cli"] == ["claude"] and entry["live_commands"] == ["claude"]
              and entry.get("model") == "claude-sonnet-5-5", entry)
        check(board.runtime_of("audit") == "claude", board.document)
    finally:
        host.close()


@c._sandbox
def test_a_running_worker_under_the_launchers_runtime_is_restarted_and_proved(tmp: Path) -> None:
    # The split with a codex session still live: the board is brought back and
    # the worker is proved under the runtime both halves now name.
    config_path, board, host = _split(tmp, live=("audit",), journal=False)
    original = host.processes["porter-audit"].pid
    try:
        result, refused, said = c._switch(config_path, host, board, "codex")
        check(result is not None and result.live_session_changed, (refused, said))
        check(board.runtime_of("audit") == "codex" and host.running("porter-audit") == "codex",
              (board.document, host.running("porter-audit")))
        check(host.processes["porter-audit"].pid != original, "the worker was not restarted")
    finally:
        host.close()


@c._sandbox
def test_the_repair_is_checked_before_anything_moves(tmp: Path) -> None:
    # Bringing the board back is a change like any other: a runtime that is not
    # ready refuses it before the board or the config is touched.
    config_path, board, host = _split(tmp)
    before = config_path.read_bytes()
    readiness = c.role_runtime._readiness_blockers
    c.role_runtime._readiness_blockers = lambda *a, **k: ["codex is not signed in for the owner account"]
    said: list[str] = []
    try:
        try:
            c.role_runtime.switch_role_runtime(
                c.team_launcher.load_project_config("porter", config_path), config_path=config_path,
                role_name="audit", runtime="codex", environ=base.DIRECTOR_ENV, runner=host, client=board,
                workflow_reader=board.reader, pane_state_dir=config_path.parent / "pane-state",
                print_func=said.append, busy_check=lambda *a, **k: False,
            )
            refused = ""
        except SystemExit as exc:
            refused = str(exc)
        check("codex is not signed in" in refused, f"the repair skipped its checks: {refused!r} {said}")
        check(board.runtime_of("audit") == "claude" and board.revision == 8 and not board.applies,
              (board.document, board.revision, board.applies))
        check(config_path.read_bytes() == before, "the config was written")
    finally:
        c.role_runtime._readiness_blockers = readiness
        host.close()


@c._sandbox
def test_the_dry_run_names_the_split_and_changes_nothing(tmp: Path) -> None:
    config_path, board, host = _split(tmp)
    before = config_path.read_bytes()
    try:
        result, refused, said = c._switch(config_path, host, board, "codex", dry_run=True)
        check(result is not None and result.configured_changed, (refused, said))
        check(result.was == "codex, while the board declared claude", result.was)
        check("switchyard: the board declares audit as claude while the launcher runs codex; "
              "this switch sets both to codex" in said, said)
        check(board.runtime_of("audit") == "claude" and board.revision == 8, "a dry run moved the board")
        check(config_path.read_bytes() == before, "a dry run wrote the config")
    finally:
        host.close()


@c._sandbox
def test_agreement_is_still_nothing_to_change(tmp: Path) -> None:
    config_path, board, host = _split(tmp, board_runtime="codex", live=("audit",), journal=False)
    try:
        applies = len(board.applies)
        result, refused, said = c._switch(config_path, host, board, "codex")
        check(result is not None and not result.configured_changed, (refused, said))
        check("already runs codex; nothing to change" in result.describe(), result.describe())
        check(len(board.applies) == applies and board.revision == 8, "an agreeing board was written")
        check(not [call for call in host.calls if call[:2] in (["tmux", "kill-session"], ["tmux", "new-session"])],
              host.calls)
    finally:
        host.close()


@c._sandbox
def test_a_board_that_does_not_name_the_role_leaves_the_launcher_to_decide(tmp: Path) -> None:
    config_path, board, host = _split(tmp, board_runtime="codex", journal=False)
    board.document["roles"] = [role for role in board.document["roles"] if role["name"] != "audit"]
    readiness = c.role_runtime._readiness_blockers
    c.role_runtime._readiness_blockers = base._ready
    try:
        checks, _document = c.role_runtime.preflight(
            c.team_launcher.load_project_config("porter", config_path), config_path=config_path,
            role_name="audit", runtime="codex", runner=host, workflow_reader=board.reader,
            busy_check=lambda *a, **k: False,
        )
        check(checks.board_runtime is None, checks)
        check(checks.is_noop, f"a role the board does not name became a change: {checks}")
    finally:
        c.role_runtime._readiness_blockers = readiness
        host.close()


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"role_runtime_board_split_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
