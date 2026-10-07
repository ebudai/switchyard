#!/usr/bin/env python3
"""SYRD-550: whether a pane holds a draft is read from its composer, not from the cursor's column.

The gate called a pane "composing" when its cursor was right of the prompt.
Live on otto, a Hermes pane resized into a wider window kept ``❯ ❯ ❯ ❯`` redraw
residue on its input line with the cursor at x=8: nothing typed, and every
notice to it held as "pane busy" for hours. The same rule failed the other way:
Claude Code indents a draft's continuation lines without a prompt, so a draft
whose current line is empty sits at x=2 and read as idle, which would have typed
a notice into it.

The Claude panes here are real (tests/fixtures/syrd550_claude_captures.json:
Claude Code 2.1.288, captured offline). The Hermes residue is the shape otto
reported, line and cursor; a Hermes TUI could not be brought up on this host
without a provider. Each case runs through the real PaneActivityGate, with
tmux standing in only as the two calls it makes (cursor, capture), on main
(2951c5c, a child from a git archive) and on this tree.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "2951c5c9bac61eaa00db14e0d16f2b8b94fe922e"  # main before SYRD-550
RULE = "─" * 120
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def pane(lines: list[str], x: int, y: int, height: int = 40) -> dict:
    lines = lines + [""] * (height - len(lines))
    return {"lines": lines, "cursor_x": x, "cursor_y": y, "height": height}


def cases() -> dict:
    claude = json.loads((ROOT / "tests/fixtures/syrd550_claude_captures.json").read_text())["captures"]
    out = {f"claude {name}": {**c, "height": len(c["lines"])} for name, c in claude.items()}
    # Hermes: otto's report -- the input line after a resize, cursor at x=8.
    out["hermes residue"] = pane(["", "  the last answer, finished", "", "❯ ❯ ❯ ❯ "], 8, 3)
    out["hermes residue under a rule"] = pane(["  the last answer", RULE, "❯ ❯ ❯ ❯ "], 8, 2)
    out["hermes residue then typed text"] = pane(["", "❯ ❯ ❯ fix the build"], 19, 1)
    out["prompted multiline draft, empty current line"] = pane(["", "❯ first line", "❯ "], 2, 2)
    out["unruled prompt, typed"] = pane(["", "› fix the build"], 15, 1)
    out["unruled prompt, empty"] = pane(["", "› "], 2, 1)
    out["output directly above an empty unruled prompt"] = pane(["", "• Ran the suite: 18 checks ok", "› "], 2, 2)
    # Codex draws its only rule as an update banner near the top: the screen
    # under it is output, not a composer (the first otto patch held every idle
    # Codex pane this way).
    out["banner rule far above an empty prompt"] = pane([RULE, " Update available", "", "Some earlier output", "", "› "], 2, 5)
    # An empty Claude box can show a placeholder right of the cursor at home.
    out["placeholder right of the cursor"] = pane([RULE, "❯\xa0Try \"fix the failing test\"", RULE], 2, 1)
    # The cursor moved up to an empty first line, the draft below it.
    out["ruled draft below the cursor"] = pane([RULE, "❯\xa0", "  second line", RULE], 2, 1)
    # Output that wrapped onto a second row above residue: `-J` would join the
    # rows and put the cursor's row on the wrong line.
    out["residue under a wrapped row"] = {**pane(["", "a long line of output that the pane wrapped onto", "the next row here", "❯ ❯ ❯ ❯ "], 8, 3),
                                          "wrapped_rows": [2]}
    # A capture that returned nothing: the column rule decides, as before.
    out["empty capture, cursor at home"] = {"lines": [], "cursor_x": 2, "cursor_y": 3, "height": 40}
    out["empty capture, cursor past home"] = {"lines": [], "cursor_x": 9, "cursor_y": 3, "height": 40}
    # Past home with nothing left of the cursor: the capture cannot show the draft.
    out["blank left of a cursor past home"] = pane([RULE, "", RULE], 9, 1)
    # A cursor below what the capture returned sits on an empty row it left out.
    out["cursor below a short capture"] = {"lines": [RULE, "❯ first line", "  second line"], "cursor_x": 2, "cursor_y": 3,
                                           "height": 40}
    return out


def scenario(root: Path) -> dict:
    sys.path.insert(0, str(root))
    from scripts.ticket_board import notify_listener as listener
    from scripts.ticket_board.pane_activity_gate import PaneActivityGate

    target = "p550-uiux:0.0"
    seen = {}
    with tempfile.TemporaryDirectory(prefix="syrd550-state.") as state:
        store = listener.PaneHookStateStore(Path(state))
        store.write(target, "idle", source="claude.Stop")
        for name, case in cases().items():
            def runner(argv, **_kwargs):
                if argv[:2] == ["tmux", "display-message"]:
                    return subprocess.CompletedProcess(argv, 0, f"{case['cursor_x']} {case['cursor_y']} {case['height']}\n", "")
                if argv[:2] == ["tmux", "capture-pane"]:
                    lines = list(case["lines"])
                    if "-J" in argv:  # tmux joins a wrapped row onto the row it continues
                        for row in sorted(case.get("wrapped_rows", []), reverse=True):
                            lines[row - 1:row + 1] = [lines[row - 1] + lines[row]]
                    return subprocess.CompletedProcess(argv, 0, "".join(line + "\n" for line in lines), "")
                raise AssertionError(f"unexpected call {argv}")
            gate = PaneActivityGate(state_store=store, client_activity_runner=runner, cursor_position_runner=runner,
                                    capture_pane_runner=runner, process_table_reader=lambda: [],
                                    sleeper=lambda _s: None, role_runtimes={"uiux": "claude"})
            gate.role_targets["uiux"] = target
            try:
                trace = gate.anti_clobber_trace(target)
            except Exception as exc:  # an answer like any other, so a check reports it
                seen[name] = ["raised", type(exc).__name__]
                continue
            seen[name] = [trace.busy, trace.reason]

        # A capture that cannot be taken at all keeps the column rule, as before.
        def no_capture(argv, **_kwargs):
            if argv[:2] == ["tmux", "display-message"]:
                return subprocess.CompletedProcess(argv, 0, "8 3 40\n", "")
            raise subprocess.CalledProcessError(1, argv)
        gate = PaneActivityGate(state_store=store, client_activity_runner=no_capture, cursor_position_runner=no_capture,
                                capture_pane_runner=no_capture, process_table_reader=lambda: [], sleeper=lambda _s: None,
                                role_runtimes={"uiux": "claude"})
        gate.role_targets["uiux"] = target
        trace = gate.anti_clobber_trace(target)
        seen["no capture, cursor at x=8"] = [trace.busy, trace.reason]
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL", "TMUX"))]:
        os.environ.pop(key)
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd550.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-2000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    composing, idle = [True, "human_composing"], [False, "hook_idle"]
    check(before["hermes residue"] == composing and before["hermes residue under a rule"] == composing,
          f"before: Hermes redraw residue held the pane as a human composing: {before['hermes residue']}")
    check(before["claude multiline_empty_current"] == idle,
          f"before: a Claude draft whose current line is empty read as idle -- a notice would be typed into it: "
          f"{before['claude multiline_empty_current']}")

    after = scenario(ROOT)
    for name in ("claude idle", "claude after_clear", "claude resized", "claude resized_back"):
        check(after[name] == idle, f"an empty Claude box is idle: {name} {after[name]}")
    check(after["claude one_line"] == composing, f"a typed Claude line is composing: {after['claude one_line']}")
    check(after["claude multiline_empty_current"] == composing,
          f"a Claude draft whose current line is empty is still composing: {after['claude multiline_empty_current']}")
    check(after["hermes residue"] == idle and after["hermes residue under a rule"] == idle,
          f"redraw residue, prompt glyphs alone, is idle: {after['hermes residue']} {after['hermes residue under a rule']}")
    check(after["hermes residue then typed text"] == composing,
          f"text typed after residue is composing: {after['hermes residue then typed text']}")
    check(after["prompted multiline draft, empty current line"] == composing,
          f"a draft whose lines are each prompted, current line empty, is composing: "
          f"{after['prompted multiline draft, empty current line']}")
    check(after["unruled prompt, typed"] == composing and after["unruled prompt, empty"] == idle,
          f"an unruled composer: typed is composing, empty is idle: {after['unruled prompt, typed']} {after['unruled prompt, empty']}")
    check(after["ruled draft below the cursor"] == composing,
          f"draft text below the cursor, inside the rules, is composing: {after['ruled draft below the cursor']}")
    check(after["residue under a wrapped row"] == idle,
          f"rows are read unjoined, so the cursor's row is the residue: {after['residue under a wrapped row']}")
    check(after["output directly above an empty unruled prompt"] == idle,
          f"output is not a draft line: {after['output directly above an empty unruled prompt']}")
    check(after["banner rule far above an empty prompt"] == idle,
          f"a banner rule far above is not a composer's: {after['banner rule far above an empty prompt']}")
    check(after["placeholder right of the cursor"] == idle,
          f"an empty box's placeholder is not a draft: {after['placeholder right of the cursor']}")
    check(after["empty capture, cursor at home"] == idle and after["empty capture, cursor past home"] == composing,
          f"an empty capture falls back to the column rule: {after['empty capture, cursor at home']} "
          f"{after['empty capture, cursor past home']}")
    check(after["blank left of a cursor past home"] == composing,
          f"blank, not glyphs, left of a cursor past home stays composing: {after['blank left of a cursor past home']}")
    check(after["cursor below a short capture"] == composing,
          f"rows a capture leaves out are empty rows; the draft above still counts: {after['cursor below a short capture']}")
    check(after["no capture, cursor at x=8"] == composing == before["no capture, cursor at x=8"],
          f"with no capture at all the column rule stands, as before: {after['no capture, cursor at x=8']}")
    changed = sorted(name for name in after if after[name] != before[name])
    check(changed == ["claude multiline_empty_current", "cursor below a short capture", "hermes residue",
                      "hermes residue under a rule", "prompted multiline draft, empty current line",
                      "residue under a wrapped row", "ruled draft below the cursor"],
          f"only the two defects' cases change verdict: {changed}")
    print(f"pane_composer_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
