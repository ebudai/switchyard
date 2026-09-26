"""Reading a provider CLI's terminal output: what it shows, and whether it is asking something.

During a provider's interactive first run, Switchyard watches the terminal and
has to tell three states apart:
- the provider is asking the person something (an answer prompt, a menu of
  choices, an input box);
- it is waiting on a sign-in finished in a browser elsewhere;
- it is ready.

It must not close the window on a question. Everything here is pure text
reading, with no I/O:
- `_visible_text` and `_visible_lines` strip escape sequences and invisible
  controls;
- `_draws_something` tells a screen that draws from one that only moves the
  cursor;
- `provider_is_waiting_for_an_answer` gives three independent readings, each
  only ever evidence FOR a question;
- `_screen_is_settled` decides when a finished step may be closed;
- `_TerminalStream` re-cuts pty reads so no escape sequence straddles two
  chunks;
- `_replaced_frame_starts_at` finds where a redrawn frame begins.

Driving the provider's session, owning the terminal and the tmux window is the
session runner, which stays in `scripts/team_launcher.py` and reaches these
through it.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-298). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module depends on nothing of Switchyard's.
"""

from __future__ import annotations

import re
from typing import Sequence


#: Terminal control sequences, taken out before any of this reads a screen: a
#: provider draws its interface with them, and matching against the raw stream
#: would match escape codes as often as words.
#: Every escape sequence a provider draws with, so what is left is what a
#: person would see. CSI parameters are the whole ECMA-48 range `0-?`, not only
#: digits: Claude's keyboard and key-reporting modes are `ESC[<u`, `ESC[>5u` and
#: `ESC[>4;2m`, and a class of `[0-9;?]` left all three behind as "text". So did
#: the charset designation `ESC(B`. That is not tidiness either -- a chunk made
#: of nothing else was read as a new screen, which is how test8's sign-in box
#: was forgotten while the User was in the browser (SYRD-221 UAT).
_ANSI_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[()*+\-./][ -~]"
    r"|\x1b[0-~]"
)


#: Control characters that move nothing a person can read -- shift-in after a
#: charset reset, the bell. Whitespace is left to the callers, who each decide
#: what a line is.
_INVISIBLE_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


#: Output a provider prints while it is waiting for the PERSON, rather than
#: sitting ready for work. Quiet alone cannot mean "finished": an OAuth code box
#: and a theme picker are both perfectly silent while somebody reads them, and
#: ending a step there would cut the User off mid-answer, which is worse than
#: the wait it replaces.
#: The glyphs these CLIs put beside the option a person is on. Claude uses the
#: first for both its menu cursor and its empty input box, which is why what
#: follows it is what matters rather than its presence.
PROVIDER_SELECTION_CURSORS: tuple[str, ...] = ("\u276f", "\u203a")


#: A menu line: a number, a dot, and the option. No ordinary prompt draws one,
#: and a code listing's line numbers have no dot after them.
_NUMBERED_OPTION = re.compile(r"^\d+\.\S")


PROVIDER_PENDING_ANSWER_MARKERS: tuple[str, ...] = (
    "paste code here",
    "press enter to continue",
    "select a theme",
    "choose a theme",
    "do you trust",
    "yes, proceed",
    "(y/n)",
    "[y/n]",
    "enter to confirm",
    "authorization code",
)


def _visible_text(raw: str) -> str:
    """Terminal output reduced to the letters it puts on the screen.

    Whitespace goes too, and that is not tidiness. These interfaces position
    every word with a cursor-move rather than with spaces, so a real Claude
    OAuth box arrives as `Paste<ESC>[8Gcode<ESC>[13Ghere`: strip the escapes
    alone and "paste code here" is nowhere in it. Measured against bytes
    captured from the live flow, not imagined (SYRD-211).
    """
    return "".join(_INVISIBLE_CONTROLS.sub("", _ANSI_ESCAPE.sub("", raw)).split()).casefold()


def _draws_something(chunk: str) -> bool:
    """Does this output put anything on the screen a person could read?

    A provider also writes output that only reconfigures the terminal. Claude
    does it every time the window loses or regains focus: `ESC(B SI ESC[<u
    ESC[>5u ESC[>4;2m`, twenty bytes that draw nothing. Recorded against the
    real CLI sat on its sign-in box -- the focus-out arrives the moment the
    person switches to the browser to sign in, which is the one moment the box
    has to be remembered. A chunk like that is not a new screen (SYRD-221 UAT).
    """
    return bool(_visible_text(chunk))


#: Output after which nothing drawn before it is on the screen any more,
#: however soon it arrives. A clock cannot say that: a person who answers
#: within a second of the question being drawn left it in the screen being
#: judged, so the step waited for an answer already given (SYRD-221 DAT).
#: Every alternative is one a provider was recorded sending, or the terminal's
#: own unambiguous equivalent:
#:
#: * `ESC[2K ESC[1A`, twice or more -- erase this line, move up. Ink's
#:   `log-update` erases its previous frame that way, and it is exactly what
#:   Claude sends when its trust question is answered. No recorded drawing
#:   screen contains the pair even once;
#: * erase the whole display, as the auto-mode prompt opens with;
#: * switch to or from the alternate screen;
#: * a full terminal reset.
_FRAME_REPLACED = re.compile(
    r"(?:\x1b\[2K\x1b\[1A){2,}(?:\x1b\[2K)?"
    r"|\x1b\[[23]J"
    r"|\x1b\[\?(?:1049|1047|47)[hl]"
    r"|\x1bc"
)


def _replaced_frame_starts_at(chunk: str) -> int | None:
    """Where the newest frame in `chunk` begins, if the chunk replaced one."""
    end = None
    for match in _FRAME_REPLACED.finditer(chunk):
        end = match.end()
    return end


def _screen_is_settled(recent: str, *, awaiting_redraw: bool) -> bool:
    """Is the current screen something a finished step can be closed at?

    Nothing drawn yet counts, which is how a provider that records itself
    before printing anything has always been closed. A screen that was just
    erased and not yet redrawn does not: that is the moment between a question
    and whatever replaces it, and `/exit` typed into it lands on neither.

    Those two look the same from `recent` alone -- both can be empty, because
    a read may end exactly where the erasing sequence does -- so the caller
    says which it is. Inferring it from an empty string sent `/exit` before
    the prompt had been drawn (SYRD-221 DAT on 34163a7).
    """
    if awaiting_redraw:
        return False
    if not recent:
        return True
    return _draws_something(recent) and not provider_is_waiting_for_an_answer(recent)


#: The longest unfinished escape worth holding for the next read. A window
#: title is the longest thing a provider sends this way; anything past this is
#: not a sequence still arriving but a stream that never finishes one, and is
#: released rather than held forever.
_ESCAPE_CARRY_LIMIT = 4096


_COMPLETE_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _unfinished_escape(tail: str) -> bool:
    """Is `tail` -- text from its last ESC onward -- an escape still arriving?"""
    if tail == "\x1b":
        return True
    if tail.startswith("\x1b["):
        return not _COMPLETE_CSI.match(tail)
    if tail.startswith("\x1b]"):
        return "\x07" not in tail and "\x1b\\" not in tail
    # A charset designation is three bytes; `ESC(` alone is waiting for its last.
    return len(tail) == 2 and tail[1] in "()*+-./"


class _TerminalStream:
    """Terminal output re-cut so no escape sequence straddles two pieces.

    A pty read ends wherever the kernel's buffer did, not at a sequence
    boundary, so a read can end in the middle of `ESC[>4;2m`. Read on its own,
    each half leaves printable residue -- `[<u`, `(B`, `>4;2m` -- and 15 of the
    19 ways to split Claude's twenty-byte focus reassertion were read as a
    screen being drawn, which is the test8 failure again by another route (SYRD-221
    DAT). Anything that reads output per chunk reads it through this.
    """

    def __init__(self) -> None:
        self._carry = ""

    def feed(self, chunk: str) -> str:
        """The part of the stream so far that ends on a whole sequence."""
        text = self._carry + chunk
        self._carry = ""
        start = text.rfind("\x1b")
        # An OSC is the one sequence with an ESC inside it: its `ESC \\`
        # terminator. A read that ends on that bare ESC makes the last ESC the
        # terminator's, not the sequence's, and holding only it released the
        # title before it as text (SYRD-221 DAT on 4f43684). So an OSC with no
        # terminator yet is held from its OWN opening, wherever the last ESC is.
        osc = text.rfind("\x1b]")
        if osc != -1 and _unfinished_escape(text[osc:]):
            start = osc
        if start != -1:
            tail = text[start:]
            if len(tail) <= _ESCAPE_CARRY_LIMIT and _unfinished_escape(tail):
                text, self._carry = text[:start], tail
        return text


def _visible_lines(raw: str) -> list[str]:
    """The same reduction as `_visible_text`, but keeping the lines.

    Structure needs lines. `_visible_text` deliberately throws whitespace away
    so a word positioned by cursor-moves still matches a phrase, and that also
    throws away which line each word was on -- which is the whole signal here.
    """
    lines = []
    for line in _INVISIBLE_CONTROLS.sub("", _ANSI_ESCAPE.sub("", raw)).splitlines():
        collapsed = "".join(line.split())
        if collapsed:
            lines.append(collapsed)
    return lines


def _provider_screen_offers_a_choice(recent_output: str) -> bool:
    """Is this screen a list of options with one of them selected?

    Asked of the shape rather than the wording, because the wording is the
    vendor's and it changes. Claude Code v2.1.270 opens its first run with
    "Choose the text style that looks best with your terminal" -- a phrase list
    written against an earlier release looked for "select a theme", matched
    nothing, and Switchyard closed the window while the question was still on
    it (SYRD-221 live UAT).

    Two shapes, both of which an ordinary prompt does not have:

    * a selection cursor sitting on an option. The cursor alone is not enough
      -- Claude draws the same glyph for its empty input box -- so it counts
      only when there is something after it, which is the highlighted choice;
    * a numbered menu, which nothing at an ordinary prompt draws.
    """
    lines = _visible_lines(recent_output)
    for index, line in enumerate(lines):
        for cursor in PROVIDER_SELECTION_CURSORS:
            if line.startswith(cursor) and line[len(cursor):]:
                if _is_input_box(lines, index):
                    continue
                return True
    return sum(1 for line in lines if _NUMBERED_OPTION.match(line)) >= 2


#: A line that is nothing but a horizontal rule -- the edges of Claude's box.
_HORIZONTAL_RULE = re.compile(r"^[─━═-]{10,}$")


def _is_input_box(lines: Sequence[str], index: int) -> bool:
    """Is the cursor line at `index` Claude's input box rather than a menu?

    Claude 2.1.278's EMPTY input box shows a suggestion after its cursor --
    `❯ Try "fix lint errors"` -- so "a cursor with something after it" reads its
    ordinary prompt as a highlighted choice, and a step closed only at a prompt
    never closes. Recorded after the trust question is answered (SYRD-221 DAT).
    The box is drawn between two horizontal rules; every recorded menu's
    cursor sits among its sibling options instead.
    """
    above = lines[index - 1] if index > 0 else ""
    below = lines[index + 1] if index + 1 < len(lines) else ""
    return bool(_HORIZONTAL_RULE.match(above) and _HORIZONTAL_RULE.match(below))


#: A screen that has handed the person a sign-in to complete somewhere else.
#: It is quiet, it asks nothing, and it is the furthest thing from finished:
#: `codex login` prints its URL and then waits on a browser callback that may
#: be minutes away. Reading that as "at its ordinary prompt" is what killed the
#: login mid-flow on test7 and launched three Codex roles with no credentials
#: (SYRD-221).
#: Every one of these is a line from a recording in `tests/fixtures`, not a
#: wording written from memory. This ticket has been reopened twice over
#: guessed phrases, and a marker nobody has seen is a guess however plausible
#: it reads. Claude's own sign-in is already caught by the OAuth box in
#: `PROVIDER_PENDING_ANSWER_MARKERS`, so it needs nothing here.
#:
#: Two lines of the same screen, deliberately: either could be reworded, and
#: the cost of the other still matching is that Switchyard waits for somebody
#: who has already finished, which is the side to be wrong on.
PROVIDER_WAITING_ON_SIGN_IN_MARKERS = (
    "starting local login server",
    "navigate to this url to authenticate",
)


def _provider_screen_is_waiting_on_a_sign_in(recent_output: str) -> bool:
    """Has the provider handed the sign-in to a browser and gone quiet?

    Evidence FOR waiting, like every other reading here. Nothing about a
    sign-in screen distinguishes it from an idle prompt by silence alone --
    that is precisely the shape that has to be recognised by what it says.
    """
    text = _visible_text(recent_output)
    return any(
        "".join(marker.split()).casefold() in text
        for marker in PROVIDER_WAITING_ON_SIGN_IN_MARKERS
    )


def provider_is_waiting_for_an_answer(recent_output: str) -> bool:
    """Is this screen asking the person something, or is it ready?

    Read from what the provider actually printed rather than from a clock.

    Three independent readings, because each alone has been wrong here. The
    phrases catch a question with no visible structure -- an OAuth box is just
    "Paste code here if prompted >" -- the structure catches a question whose
    wording has moved on, which is what stranded a fresh tenant, and the
    sign-in markers catch a screen that is not asking anything at all because
    it is waiting on a browser somewhere else.

    Both are evidence FOR a question, never against one, and that asymmetry is
    deliberate: a false positive makes Switchyard wait a little longer for
    somebody who has already finished, and a false negative closes the window
    while they are still reading it.
    """
    text = _visible_text(recent_output)
    if any(
        "".join(marker.split()).casefold() in text
        for marker in PROVIDER_PENDING_ANSWER_MARKERS
    ):
        return True
    if _provider_screen_is_waiting_on_a_sign_in(recent_output):
        return True
    return _provider_screen_offers_a_choice(recent_output)
