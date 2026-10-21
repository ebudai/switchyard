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

A rule is a composer's only while the cursor can be inside it. Codex 0.162
draws its update banner as a closed box at the top of the pane, and the box's
bottom border read as the top rule of a composer holding the Codex header
under it: every idle Codex pane that showed the banner held its notices as
"human_composing" (MEFP-51 on luna-9, SYRD-591). The border that closes a box
above the cursor ends that box, so it does not bound a composer the cursor is
in. A cursor inside a box is still inside a ruled composer.

Codex does not rule its composer: a draft's first row is prompted and its
further rows are indented under it. A cursor at home on an empty row of its
own under such rows is on the draft's next line, as it is under prompted rows.

Copilot can draw its input as a filled box: a top edge ``╻▄▄▄…``, every input
row behind a heavy bar ``┃``, a bottom edge ``╹▀▀▀…``, and no prompt glyph.
Live on syrd's Inspector (Copilot 1.0.101, signed in, Allow All) the empty box
was the row ``┃`` with the cursor at x=2, and the bar read as draft text: every
notice to the idle pane was held as "human_composing" (SYRD-640). The bar is
the box's, not the draft's, on a row the corners prove is inside the box the
cursor is in; whatever follows it is the draft, on the cursor's row too, right
of the cursor as well as left: the empty box shows no placeholder. Its earlier
prompts in the transcript are ``▄``/``▀`` blocks without those corners, and
are output.
"""

from __future__ import annotations

#: Prompt glyphs measured on live panes: Claude Code ``❯`` + NBSP, Hermes ``❯`` +
#: space, Gemini ``>`` + space; ``›`` ``▸`` ``❱`` are defensive.
PROMPT_GLYPHS = frozenset("❯>›▸❱")
RULE_CHAR = "─"
RULE_MIN = 40
#: Box corners, square and rounded: Codex's update banner is a rounded box (SYRD-591).
BOX_TOP_CORNERS = frozenset("╭┌╮┐")
BOX_BOTTOM_CORNERS = frozenset("╰└╯┘")
#: Copilot's filled input box (SYRD-640): its edges' corners and fill, and the bar in front of each input row.
INPUT_BOX_TOP, INPUT_BOX_TOP_FILL = "╻", "▄"
INPUT_BOX_BOTTOM, INPUT_BOX_BOTTOM_FILL = "╹", "▀"
INPUT_BOX_BAR = "┃"


def _is_rule(line: str) -> bool:
    return line.count(RULE_CHAR) >= RULE_MIN


def _has_corner(line: str, corners: frozenset[str]) -> bool:
    stripped = line.strip()
    return bool(stripped) and (stripped[0] in corners or stripped[-1] in corners)


def box_border(line: str) -> bool:
    """A box's top or bottom edge: the line has a corner at either end."""
    return _has_corner(line, BOX_TOP_CORNERS | BOX_BOTTOM_CORNERS)


def _bounding_rules(lines: list[str], cursor_y: int) -> tuple[int | None, int | None]:
    """The nearest rules above and below the cursor; None for a side with none.

    A box's bottom border above the cursor closed that box: it is no rule of
    a composer the cursor is in.
    """
    above = next((i for i in range(cursor_y - 1, -1, -1) if _is_rule(lines[i])), None)
    below = next((i for i in range(cursor_y + 1, len(lines)) if _is_rule(lines[i])), None)
    if above is not None and _has_corner(lines[above], BOX_BOTTOM_CORNERS):
        above = None
    return above, below


def _input_box_edge(line: str, corner: str, fill: str) -> bool:
    stripped = line.strip()
    return stripped[:1] == corner and stripped.count(fill) >= RULE_MIN


def _input_box(lines: list[str], cursor_y: int) -> range | None:
    """The input rows of the filled box the cursor is in, every one behind the bar; None when it is in none."""
    def barred(row: int) -> bool:
        return lines[row].lstrip().startswith(INPUT_BOX_BAR)

    if not barred(cursor_y):
        return None
    top = next((row for row in range(cursor_y - 1, -1, -1) if not barred(row)), None)
    bottom = next((row for row in range(cursor_y + 1, len(lines)) if not barred(row)), None)
    if top is None or bottom is None or not _input_box_edge(lines[top], INPUT_BOX_TOP, INPUT_BOX_TOP_FILL) \
            or not _input_box_edge(lines[bottom], INPUT_BOX_BOTTOM, INPUT_BOX_BOTTOM_FILL):
        return None
    return range(top + 1, bottom)


def _input_box_texts(lines: list[str], rows: range) -> list[str]:
    """Each input row's text, the bar in front of it set aside.

    The whole row, the cursor's too: the empty box shows no placeholder right of the cursor, so text there is a
    draft whose cursor was moved back into it.
    """
    return [_text(lines[row].lstrip()[len(INPUT_BOX_BAR):]) for row in rows]


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


def _draft_rows_above(lines: list[str], cursor_y: int, left: str) -> list[int]:
    """The rows of an unruled draft directly above the cursor's row, top first; [] when there is none.

    Prompted rows that hold text, as each line of a prompted draft is. Or,
    when nothing left of the cursor is a prompt, indented rows that hold text
    under a prompted row that does: Codex's continuation lines (SYRD-591).
    """
    row = cursor_y - 1
    rows: list[int] = []
    while row >= 0 and _prompted(lines[row]) and _text(lines[row]):
        rows.insert(0, row)
        row -= 1
    if rows or any(ch in PROMPT_GLYPHS for ch in left):
        return rows
    while row >= 0 and lines[row][:1].isspace() and not _prompted(lines[row]) and _text(lines[row]):
        rows.insert(0, row)
        row -= 1
    if rows and row >= 0 and _prompted(lines[row]) and _text(lines[row]):
        return [row] + rows
    return []


def composing(lines: list[str], cursor_x: int, cursor_y: int, home_x: int = 2) -> bool | None:
    """True when the composer holds text, False when it holds none, None when the capture cannot say."""
    if cursor_y < 0 or cursor_x < 0:
        return None
    # A capture can leave out the empty rows at the bottom of the pane; a cursor
    # below what it returned sits on one of those. An empty capture is all such
    # rows, which the column rule below then decides, as it always did.
    lines = list(lines) + [""] * (cursor_y + 1 - len(lines))
    if (box := _input_box(lines, cursor_y)) is not None:
        return any(_input_box_texts(lines, box))
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

    above, below = _bounding_rules(lines, cursor_y)
    ruled = above is not None and (below is not None or all(_composer_shaped(l) for l in lines[above + 1:cursor_y]))
    if ruled:
        others = lines[above + 1:cursor_y] + (lines[cursor_y + 1:below] if below is not None else [])
        return bool(cursor_text) or any(_text(line) for line in others)
    if cursor_text:
        return True
    # Unruled: an empty line under a draft's rows is the draft's next line.
    return bool(_draft_rows_above(lines, cursor_y, left))


def composer_text(lines: list[str], cursor_x: int, cursor_y: int) -> str | None:
    """What the composer holds, prompt glyphs and indentation set aside (SYRD-565).

    '' when it holds nothing, None when the capture cannot place the cursor.
    The same composer `composing` reads: a ruled box's rows, or else the cursor
    row and the rows of a draft directly above it. On the cursor's own
    row only what is left of the cursor counts -- right of it is where an empty
    box shows its placeholder -- so the text a notice just typed, which leaves
    the cursor at its end, is read whole.
    """
    if cursor_y < 0 or cursor_x < 0:
        return None
    lines = list(lines) + [""] * (cursor_y + 1 - len(lines))
    if (box := _input_box(lines, cursor_y)) is not None:
        return "\n".join(text for text in _input_box_texts(lines, box) if text)
    above, below = _bounding_rules(lines, cursor_y)
    ruled = above is not None and (below is not None or all(_composer_shaped(l) for l in lines[above + 1:cursor_y]))
    if ruled:
        rows = list(range(above + 1, below if below is not None else cursor_y + 1))
    else:
        rows = _draft_rows_above(lines, cursor_y, lines[cursor_y][:cursor_x]) + [cursor_y]
    texts = [_text(lines[r][:cursor_x]) if r == cursor_y else _text(lines[r]) for r in rows]
    return "\n".join(text for text in texts if text)
