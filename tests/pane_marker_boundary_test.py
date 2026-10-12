#!/usr/bin/env python3
"""SYRD-444: a pane marker matches its own pane, never a longer numbered one.

A pane's env wrapper carries `TICKET_BOARD_PANE_TARGET=<session>:<window>.<pane>`
(and `PGU_PANE_TARGET=` for a `pgu-` target) as one argument. `_role_has_pane_process`
searched `ps -eo args` lines for it as a substring, so the role on `p444:0.1`
also matched a process marked `p444:0.10`. The marker is now matched as a whole
word: start of line, whitespace or shell punctuation on each side.

The commands come from the real `cli_command_for_role`, joined as `ps` prints
argv. No process is listed and none is signalled; liveness is asked with a
tmux target set and assignments this test writes.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _config(tmp: Path, targets: dict[str, str], project: str = "p444"):
    leaf = lambda index: {"Command": "", "SessionRestoreId": index, "Title": "", "WorkingDirectory": ""}
    layout = tmp / "layout.json"
    layout.write_text(json.dumps({"Orientation": "Horizontal",
                                  "Widgets": [leaf(n) for n in range(len(targets))]}) + "\n", encoding="utf-8")
    for role in targets:
        (tmp / "work" / role).mkdir(parents=True, exist_ok=True)
    path = tmp / f"{project}.json"
    path.write_text(json.dumps({
        "desktop_access": {"mode": "headless"}, "project": project, "layout": str(layout),
        "roles": [{"role": role, "slot": slot, "cli": ["claude"], "target": target, "workdir": str(tmp / "work" / role)}
                  for slot, (role, target) in enumerate(targets.items())],
    }) + "\n", encoding="utf-8")
    return launcher.load_project_config(project, path)


def _ps_line(role, tmp: Path) -> tuple[list[str], str]:
    """The role's real pane command, and the line `ps -eo args` shows for it."""
    argv = [str(part) for part in launcher.cli_command_for_role(
        role, session_dir=tmp / "sessions" / role.role, pane_state_dir=tmp / "pane-state" / role.role)]
    return argv, " ".join(argv)


def test_a_real_pane_marker_is_its_own_pane_only() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd444-real.") as raw:
        tmp = Path(raw)
        config = _config(tmp, {"one": "p444:0.1", "ten": "p444:0.10", "eleven": "p444:0.11"})
        one, ten, eleven = config.roles
        argv_one, line_one = _ps_line(one, tmp)
        _, line_ten = _ps_line(ten, tmp)
        _, line_eleven = _ps_line(eleven, tmp)
    check("TICKET_BOARD_PANE_TARGET=p444:0.1" in argv_one,
          f"the marker is one whole argument of the pane's env wrapper: {argv_one[:6]}")
    check(launcher._role_has_pane_process(one, [line_one]), "pane 1's own process is pane 1's")
    check(launcher._role_has_pane_process(ten, [line_ten]), "pane 10's own process is pane 10's")
    check(not launcher._role_has_pane_process(one, [line_ten]), f"pane 10's process is not pane 1's: {line_ten[:120]}")
    check(not launcher._role_has_pane_process(one, [line_ten, line_eleven]),
          "nor is pane 11's, nor both together")
    check(not launcher._role_has_pane_process(ten, [line_one]), "and pane 1's is not pane 10's")
    check(launcher._role_has_pane_process(one, [line_ten, line_one, line_eleven]),
          "among its neighbours, pane 1's own process is still found")


def test_the_legacy_marker_has_the_same_boundary() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd444-legacy.") as raw:
        tmp = Path(raw)
        config = _config(tmp, {"one": "pgu-x:0.1", "ten": "pgu-x:0.10"}, project="pgu")
        one, ten = config.roles
        argv_ten, line_ten = _ps_line(ten, tmp)
    legacy_only = " ".join(part for part in argv_ten if not part.startswith("TICKET_BOARD_PANE_TARGET="))
    check("PGU_PANE_TARGET=pgu-x:0.10" in argv_ten and "PGU_PANE_TARGET" in legacy_only,
          f"a pgu- target also carries the legacy marker: {argv_ten[:8]}")
    check(launcher._role_has_pane_process(ten, [legacy_only]), "the legacy marker alone still names its pane")
    check(not launcher._role_has_pane_process(one, [line_ten]) and not launcher._role_has_pane_process(one, [legacy_only]),
          "and neither marker of pane 10 is pane 1's")


def test_valid_shapes_are_still_recognised() -> None:
    role = SimpleNamespace(role="one", target="p444:0.1")
    shapes = {
        "the marker as an argument": "claude TICKET_BOARD_PANE_TARGET=p444:0.1 --resume",
        "the marker last": "env TICKET_BOARD_PANE_TARGET=p444:0.1",
        "the marker alone": "TICKET_BOARD_PANE_TARGET=p444:0.1",
        "the legacy marker": "env PGU_PANE_TARGET=p444:0.1 codex",
        "inside a sh -c string": "sh -c exec env TICKET_BOARD_PANE_TARGET=p444:0.1 claude",
        "before a semicolon": "sh -c env TICKET_BOARD_PANE_TARGET=p444:0.1;claude",
        "quoted in a shell string": "bash -lc 'TICKET_BOARD_PANE_TARGET=p444:0.1' claude",
        "an unparsable command": "sh -c 'x TICKET_BOARD_PANE_TARGET=p444:0.1",
        "a tab before it": "env\tTICKET_BOARD_PANE_TARGET=p444:0.1\tclaude",
        "after a longer one on the same line": "sh -c env TICKET_BOARD_PANE_TARGET=p444:0.10 true; "
                                               "exec env TICKET_BOARD_PANE_TARGET=p444:0.1 claude",
    }
    found = {name: launcher._role_has_pane_process(role, [line]) for name, line in shapes.items()}
    check(all(found.values()), f"every shape a pane's command really takes is recognised: {found}")


def test_a_longer_or_different_marker_is_not_this_pane() -> None:
    role = SimpleNamespace(role="one", target="p444:0.1")
    others = {
        "a longer pane number": "claude TICKET_BOARD_PANE_TARGET=p444:0.10",
        "a longer legacy pane number": "env PGU_PANE_TARGET=p444:0.12 codex",
        "a deeper target": "claude TICKET_BOARD_PANE_TARGET=p444:0.1.2",
        "a word continuing it": "claude TICKET_BOARD_PANE_TARGET=p444:0.1x",
        "a path continuing it": "claude TICKET_BOARD_PANE_TARGET=p444:0.1/x",
        "a variable whose name ends in it": "env XTICKET_BOARD_PANE_TARGET=p444:0.1 claude",
        "a legacy name with a prefix": "env OLD_PGU_PANE_TARGET=p444:0.1 claude",
        "another session's pane": "claude TICKET_BOARD_PANE_TARGET=p444x:0.1",
        "the dot taken as a wildcard": "claude TICKET_BOARD_PANE_TARGET=p444:0x1",
        "a tmux command carrying it": "tmux new -e TICKET_BOARD_PANE_TARGET=p444:0.1",
        "an unparsable tmux command": "tmux 'x TICKET_BOARD_PANE_TARGET=p444:0.1",
        "blank": "   ",
    }
    found = {name: launcher._role_has_pane_process(role, [line]) for name, line in others.items()}
    check(not any(found.values()), f"none of these is pane 1's process: {found}")
    check(not launcher._role_has_pane_process(role, []), "and no command at all is no process")


def test_liveness_and_assignment_never_take_the_neighbouring_pane() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd444-live.") as raw:
        tmp = Path(raw)
        config = _config(tmp, {"one": "p444:0.1", "ten": "p444:0.10"})
        one = config.roles[0]
        ask = lambda targets, assignments: launcher.pane_liveness(
            config, one, tmux_targets=set(targets), assignments=assignments, owner_uid=None, proc_root=tmp / "proc")
        only_ten = ask({"p444:0.10"}, {})
        assigned_ten = ask({"p444:0.1", "p444:0.10"}, {"one": {"actual_target": "p444:0.10", "process_pid": 1}})
        own = ask({"p444:0.1", "p444:0.10"}, {})
    check(not only_ten.live and "no pane p444:0.1 " in only_ten.why,
          f"tmux holding only pane 10 does not make pane 1 live: {only_ten}")
    check(not assigned_ten.live and "p444:0.10, not p444:0.1" in assigned_ten.why,
          f"an assignment to pane 10 is not pane 1's: {assigned_ten}")
    check(own.live, f"and pane 1's own target is live: {own}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"pane_marker_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
