"""Is someone composing in this pane? Read from what the composer shows (SYRD-550).

The gate used to answer from the cursor alone: right of the prompt meant a
human was typing. Both directions were wrong on real panes:

* redraw residue: Hermes's prompt_toolkit, resized into a wider window, left
  ``❯ ❯ ❯ ❯`` on its input line with the cursor at x=8. Nothing was typed, but
  every notice to the pane was held as "pane busy" for hours (otto OTTO-13,
  OTTO-71, OTTO-79);
* a multiline draft: Claude Code's continuation lines are indented, not
  prompted, so a draft whose current line is empty has its cursor at x=2, home,
  and read as not composing -- a notice would be typed into the half-written
  message.

So the composer's own content decides. A ruled composer (Claude Code draws a
rule above and below it) is composing when any line in it holds text once the
prompt glyphs are set aside, wherever the cursor is. An unruled one is composing
when there is text left of the cursor, or when the cursor sits on an empty
prompt line under a prompted line that holds text. Prompt glyphs and whitespace
alone are never text, however many there are.

Two allowances keep what was already true. Text to the RIGHT of the cursor is
not counted on its own line, because that is where an empty box shows its
placeholder. And a cursor past the prompt's home with nothing at all left of it
-- a capture that does not show what the cursor is in -- is read as the column
rule always read it, composing. Holding a notice can only delay it; typing into
a draft destroys it.
"""

from __future__ import annotations

#: Prompt glyphs measured on live panes: Claude Code ``❯`` + NBSP, Hermes ``❯`` +
#: space, Gemini ``>`` + space; ``›`` ``▸`` ``❱`` are defensive.
PROMPT_GLYPHS = frozenset("❯>›▸❱")
RULE_CHAR = "─"
RULE_MIN = 40


def _is_rule(line: str) -> bool:
    return line.count(RULE_CHAR) >= RULE_MIN


def _text(line: str) -> str:
    """The line less its leading run of prompt glyphs and whitespace (NBSP included)."""
    index = 0
    while index < len(line) and (line[index] in PROMPT_GLYPHS or line[index].isspace()):
        index += 1
    return line[index:].strip()


def _prompted(line: str) -> bool:
    stripped = line.lstrip()
    return bool(stripped) and stripped[0] in PROMPT_GLYPHS


def _composer_shaped(line: str) -> bool:
    """A line that can belong to a composer: blank, prompted, or an indented continuation."""
    return not line.strip() or _prompted(line) or line[:1].isspace()


def composing(lines: list[str], cursor_x: int, cursor_y: int, home_x: int = 2) -> bool | None:
    """True when the composer holds text, False when it holds none, None when the capture cannot say."""
    if cursor_y < 0 or cursor_x < 0:
        return None
    # A capture can leave out the empty rows at the bottom of the pane; a cursor
    # below what it returned sits on one of those. An empty capture is all such
    # rows, which the column rule below then decides, as it always did.
    lines = list(lines) + [""] * (cursor_y + 1 - len(lines))
    cursor_line = lines[cursor_y]
    # On the cursor's own line only what is left of it counts: right of a
    # cursor at home is where an empty box shows its placeholder, and right of
    # one past typed text adds nothing (the typed text already counts).
    left = cursor_line[:cursor_x]
    cursor_text = _text(left)
    if not cursor_text and cursor_x > home_x and not any(ch in PROMPT_GLYPHS for ch in left):
        # Past the prompt's home with nothing at all to its left: the capture
        # does not show what the cursor is in. Residue is prompt glyphs; this
        # is not, so it is read as the column rule always read it.
        return True

    above = next((i for i in range(cursor_y - 1, -1, -1) if _is_rule(lines[i])), None)
    below = next((i for i in range(cursor_y + 1, len(lines)) if _is_rule(lines[i])), None)
    ruled = above is not None and (below is not None or all(_composer_shaped(l) for l in lines[above + 1:cursor_y]))
    if ruled:
        others = lines[above + 1:cursor_y] + (lines[cursor_y + 1:below] if below is not None else [])
        return bool(cursor_text) or any(_text(line) for line in others)
    if cursor_text:
        return True
    # Unruled: an empty prompt line under a prompted line that holds text is the
    # next line of a draft whose lines are each prompted.
    previous = lines[cursor_y - 1] if cursor_y > 0 else ""
    return _prompted(previous) and bool(_text(previous))
