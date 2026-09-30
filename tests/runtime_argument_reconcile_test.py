#!/usr/bin/env python3
"""A runtime switch carries a role's arguments to the new runtime, or refuses (SYRD-533).

MEFP's luna-6, after its Codex -> Claude switch and the SYRD-525 split repair,
was started as `claude --model claude-sonnet-5-5 --dangerously-skip-permissions
-c model_reasoning_effort="high"` with `effort` unset: the switch changed only
the program, and Claude reads `-c` as --continue and the assignment as a
prompt. These cases run the real `switch_role_runtime` over the SYRD-486
fixtures -- tmux stood in for, each session a real process named after its
CLI, a board with the SQL's revision rule -- and read the command each start
really renders.
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
from scripts import team_launcher  # noqa: E402

CHECKS = 0
CODEX_EFFORT = ["-c", 'model_reasoning_effort="high"']


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _role(tmp: Path, *, cli: str, board_runtime: str, extra_args: list[str], effort=None,
          live: tuple[str, ...] = (), dies: set[str] = frozenset()):
    config_path = c._tenant(tmp, cli=cli, live_commands=[cli])
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    for entry in raw["roles"]:
        if entry["role"] == "audit":
            entry["extra_args"] = list(extra_args)
            entry["yolo"] = True
            if effort is None:
                entry.pop("effort", None)
            else:
                entry["effort"] = effort
    config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    document = base._workflow_document()
    for role in document["roles"]:
        if role["name"] == "audit":
            role["runtime"] = board_runtime
    board = c.StrictBoard(document)
    board.revision = 8
    return config_path, board, c.Host(tmp, config_path, live=live, dies=set(dies))


def _rendered(config_path: Path) -> list[str]:
    """The fresh-start argv the launcher renders for the role, as configured now."""
    config = team_launcher.load_project_config("porter", config_path)
    role = team_launcher._role_by_name(config, "audit")
    command = team_launcher.cli_command_for_role(role, session_dir=config_path.parent / "sessions")
    return command[command.index(role.cli[0]):]


@c._sandbox
def test_mefps_shape_is_repaired_by_a_scoped_same_runtime_switch(tmp: Path) -> None:
    """Already Claude on both halves, with Codex's effort left in extra_args: dry run, then apply."""
    config_path, board, host = _role(tmp, cli="claude", board_runtime="claude", extra_args=CODEX_EFFORT)
    try:
        before = config_path.read_bytes()
        check(_rendered(config_path)[-2:] == CODEX_EFFORT, f"the reported shape: {_rendered(config_path)}")
        result, refused, said = c._switch(config_path, host, board, "claude", dry_run=True)
        check(result is not None and result.configured_changed, f"not a no-op: {refused} {said}")
        check(any("Codex effort" in line and "becomes effort" in line for line in said), said)
        check(config_path.read_bytes() == before and board.revision == 8 and not c._journal(config_path).exists(),
              "a dry run changes nothing")
        check(result.argument_repair, "and it is reported as a repair on the same runtime, not a move")
        # The recovery MEFP is told to run, parsed by the parser it really has.
        from scripts import switchyard_parsers
        for argv in (["mefp", "luna-6", "--cli", "claude", "--dry-run"], ["mefp", "luna-6", "--cli", "claude"]):
            parsed = switchyard_parsers._build_switchyard_set_role_runtime_parser().parse_args(argv)
            check((parsed.cli, getattr(parsed, "model", None), bool(getattr(parsed, "dry_run", False)))
                  == ("claude", None, "--dry-run" in argv), f"`set-role-runtime {' '.join(argv)}` parses as meant: {parsed}")

        result, refused, said = c._switch(config_path, host, board, "claude")
        check(result is not None and result.configured_changed, (refused, said))
        entry = c._entry(config_path)
        check(entry["extra_args"] == [] and entry.get("effort") == "high" and entry["cli"] == ["claude"], entry)
        rendered = _rendered(config_path)
        check("-c" not in rendered and not any("model_reasoning_effort" in arg for arg in rendered), rendered)
        check(rendered[rendered.index("--effort") + 1] == "high", f"the effort is Claude's own: {rendered}")
        check(board.runtime_of("audit") == "claude" and board.revision == 9,
              f"board agreement kept, one exact revision: {board.revision}")
        check(not c._journal(config_path).exists(), "the journal is retired after a clean switch")
        check("still runs claude" in result.describe(), f"and said so: {result.describe()}")
        result, refused, said = c._switch(config_path, host, board, "claude", dry_run=True)
        check(result is not None and not result.configured_changed, f"and repaired means a no-op now: {said}")
    finally:
        host.close()


@c._sandbox
def test_a_codex_to_claude_switch_translates_the_effort(tmp: Path) -> None:
    config_path, board, host = _role(tmp, cli="codex", board_runtime="codex",
                                     extra_args=["--verbose", *CODEX_EFFORT, "--dangerously-bypass-approvals-and-sandbox"],
                                     live=("audit",))
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    next(e for e in raw["roles"] if e["role"] == "audit")["yolo"] = False
    config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        result, refused, said = c._switch(config_path, host, board, "claude", model="claude-sonnet-5-5")
        check(result is not None and result.live_session_changed, (refused, said))
        entry = c._entry(config_path)
        check(entry["extra_args"] == ["--verbose"] and entry.get("effort") == "high", f"unrelated args stay: {entry}")
        check(entry["yolo"] is True and "--dangerously-skip-permissions" in _rendered(config_path)
              and "--dangerously-bypass-approvals-and-sandbox" not in _rendered(config_path),
              f"Codex's approval bypass becomes Claude's: {entry} {_rendered(config_path)}")
        started = [call for call in host.calls if call[:2] == ["tmux", "new-session"]
                   and call[call.index("-s") + 1] == "porter-audit"]
        check(started and "--effort high" in started[-1][-1] and "model_reasoning_effort" not in started[-1][-1],
              f"the new worker is started with Claude's effort: {started[-1][-1] if started else None}")
    finally:
        host.close()


@c._sandbox
def test_a_claude_to_codex_switch_translates_the_effort_the_other_way(tmp: Path) -> None:
    config_path, board, host = _role(tmp, cli="claude", board_runtime="claude", extra_args=["--effort", "medium"])
    try:
        result, refused, said = c._switch(config_path, host, board, "codex", model="")
        check(result is not None, (refused, said))
        entry = c._entry(config_path)
        check(entry["extra_args"] == [] and entry.get("effort") == "medium", entry)
        check(_rendered(config_path)[-2:] != ["--effort", "medium"]
              and 'model_reasoning_effort="medium"' in _rendered(config_path), _rendered(config_path))
    finally:
        host.close()


@c._sandbox
def test_same_provider_arguments_are_kept(tmp: Path) -> None:
    """Claude's own standalone -c, on a Claude role, is not Codex configuration."""
    config_path, board, host = _role(tmp, cli="claude", board_runtime="claude",
                                     extra_args=["-c", "--verbose", "--effort", "high"])
    try:
        before = config_path.read_bytes()
        result, refused, said = c._switch(config_path, host, board, "claude")
        check(result is not None and not result.configured_changed, f"nothing to change: {refused} {said}")
        check(config_path.read_bytes() == before and board.revision == 8, "and nothing changed")
    finally:
        host.close()


@c._sandbox
def test_untranslatable_configuration_is_refused_before_anything_changes(tmp: Path) -> None:
    for extra_args, effort, words in (
        (["-c", 'sandbox_mode="danger-full-access"'], None, "has no claude equivalent"),
        (CODEX_EFFORT, "low", "records effort 'low'"),
    ):
        config_path, board, host = _role(tmp / words.split()[0], cli="claude", board_runtime="claude",
                                         extra_args=extra_args, effort=effort)
        try:
            before = config_path.read_bytes()
            result, refused, said = c._switch(config_path, host, board, "claude")
            check(result is None and words in refused, f"refused with the argument named: {refused!r} {said}")
            check("extra_args[0]" in refused or "records effort" in refused, refused)
            check(config_path.read_bytes() == before and board.revision == 8 and not c._journal(config_path).exists(),
                  "and nothing was changed")
        finally:
            host.close()
    config_path, board, host = _role(tmp / "continue", cli="claude", board_runtime="claude", extra_args=["-c"],
                                     live=("audit",))
    try:
        result, refused, said = c._switch(config_path, host, board, "codex", model="")
        check(result is None and "Claude's --continue" in refused, f"Claude's -c has no Codex meaning: {refused!r}")
    finally:
        host.close()


@c._sandbox
def test_a_failed_start_restores_the_arguments_exactly(tmp: Path) -> None:
    config_path, board, host = _role(tmp, cli="codex", board_runtime="codex", extra_args=CODEX_EFFORT,
                                     live=("audit",), dies={"claude"})
    try:
        before = config_path.read_bytes()
        result, refused, said = c._switch(config_path, host, board, "claude", model="claude-sonnet-5-5")
        check(result is None, f"a worker that dies on start fails the switch: {said}")
        check(config_path.read_bytes() == before, "the rollback restores the entry, Codex arguments and all")
        check(board.runtime_of("audit") == "codex", (board.document, board.revision))
    finally:
        host.close()


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"runtime_argument_reconcile_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
