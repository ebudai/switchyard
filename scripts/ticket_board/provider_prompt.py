"""Whether a role's pane is stopped at a provider CLI's own question (SYRD-566).

A freshly started pane is recorded idle by the launcher before its CLI has drawn
anything, and Codex fires no hook until its first prompt. So until then the only
thing that can say whether the pane is ready for a ticket is its screen -- and a
screen that has stopped changing is not the same as a CLI ready for work. On otto
three of them were not:

* Codex's model-retirement notice ("Meet GPT-6 Sol ... 1. Try new model /
  2. Use existing model") stops an unattended start until somebody answers it;
* its folder-trust question and its sign-in menu do the same;
* `/new` asks "Where should the new conversation run? 1. Use current Git
  worktree / 2. Create new Git worktree", and the second moves the role out of
  its managed checkout.

Typing a ticket notice into any of them is worse than holding it: the notice
ends in Enter, which answers the question with whatever is highlighted. So a
pane showing one is never typed into, and the question is named, so a person
can answer it. Nothing here answers anything.

Every rule below comes from real screens, recorded offline in throwaway homes
(tests/fixtures/syrd566_provider_screens.json): Codex 0.162.0 and Claude Code
2.1.294. A question is a list of options, one of them under the selection
cursor, above a key-hint footer that tells you to press Enter -- "enter/esc
confirm", "enter select · esc back", "enter continue · esc back", "Press enter
to continue", "Enter to confirm · Esc to cancel". An ordinary prompt has no
such footer: Codex shows "? for shortcuts", Claude "? for shortcuts" under its
ruled box.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The selection cursor each CLI draws on the highlighted option.
CURSOR_GLYPHS = "›❯>▸❱"
_OPTION = re.compile(rf"^\s*(?P<cursor>[{CURSOR_GLYPHS}])?\s*(?:(?P<number>\d{{1,2}})\.\s+)?(?P<text>\S.*)$")
_ENTER = re.compile(r"\benter\b", re.IGNORECASE)
_HINT = re.compile(r"\b(?:esc|confirm|select|continue)\b", re.IGNORECASE)
#: How far above its footer a question's options may start.
_OPTION_ROWS = 24

PROVIDER_PROMPT_PREFIX = "provider_prompt:"


@dataclass(frozen=True)
class ProviderPrompt:
    kind: str
    options: tuple[str, ...]

    @property
    def reason(self) -> str:
        """The activity-gate reason a pane at this question is held under."""
        return PROVIDER_PROMPT_PREFIX + self.kind

    @property
    def description(self) -> str:
        return KINDS[self.kind][0]

    @property
    def remedy(self) -> str:
        return KINDS[self.kind][1]


#: What each recognised question is, and what a person does about it. Never
#: what Switchyard does: it answers none of them.
KINDS: dict[str, tuple[str, str]] = {
    "model_retirement": (
        "a model-retirement notice (its configured model is being replaced, and it asks whether to switch)",
        "answer it in the pane -- the first option switches the role to the new model, the second keeps the "
        "configured one -- or set the role to a current model and restart it",
    ),
    "folder_trust": (
        "a folder-trust question for its worktree",
        "trust the role's own worktree through the supported first-run step (`ticket-board-workflow prepare-role`), "
        "or answer it in the pane after checking the path it names is that role's managed worktree",
    ),
    "sign_in": (
        "a sign-in menu: the CLI has no credentials for this account",
        "sign the role's account in through first-run setup, then restart the role",
    ),
    "new_conversation_location": (
        "the `/new` menu asking where the new conversation should run",
        "press Esc, or choose \"Use current Git worktree\", in the pane: \"Create new Git worktree\" would move the role "
        "out of its managed checkout. Switchyard starts a fresh conversation with `/clear`, which asks nothing",
    ),
    "menu": (
        "an unanswered menu",
        "answer it in the pane",
    ),
}

#: Wording that names the question, as the recorded screens show it.
_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("model_retirement", ("try new model", "use existing model")),
    ("folder_trust", ("trust this folder", "do you trust", "i trust this folder")),
    ("sign_in", ("sign in with chatgpt", "provide your own api key", "sign in with device code", "select login method")),
    ("new_conversation_location", ("where should the new conversation run", "create new git worktree")),
)


def open_prompt(lines: list[str]) -> ProviderPrompt | None:
    """The question this screen is stopped at, or None when it is not asking one."""
    rows = [str(line).rstrip() for line in lines]
    filled = [index for index, row in enumerate(rows) if row.strip()]
    footer = next((index for index in reversed(filled[-3:]) if _is_footer(rows[index])), None)
    if footer is None:
        return None
    options = _options_above(rows, footer)
    if len(options) < 2:
        return None
    text = " ".join(rows[max(0, footer - _OPTION_ROWS):footer + 1]).lower()
    kind = next((name for name, markers in _MARKERS if any(marker in text for marker in markers)), "menu")
    return ProviderPrompt(kind, tuple(options))


def _options_above(rows: list[str], footer: int) -> list[str]:
    """The options of the list whose highlighted row is nearest above the footer.

    The list is the run of rows around the highlighted one, up to a blank row
    that does not lead to another numbered option. An option is a row whose text starts in the same column as the highlighted
    option's: Claude marks only the highlighted row ("❯ No, exit" over
    "  Yes, I trust this folder"), and Codex indents an option's description
    further than its label.
    """
    top = max(-1, footer - 1 - _OPTION_ROWS)
    cursor = next((index for index in range(footer - 1, top, -1)
                   if (match := _OPTION.match(rows[index])) and match.group("cursor")), None)
    if cursor is None:
        return []
    column = _label_column(rows[cursor])

    def continues(index: int, step: int) -> bool:
        # The list goes on through this row: it has text, or it is a gap before
        # another numbered option (Codex's sign-in menu spaces them out).
        if rows[index].strip():
            return True
        beyond = next((i for i in range(index + step, index + 3 * step, step) if top < i < footer and rows[i].strip()), None)
        return beyond is not None and _numbered_at(rows[beyond], column)

    start = cursor
    while start - 1 > top and continues(start - 1, -1):
        start -= 1
    end = cursor
    while end + 1 < footer and continues(end + 1, 1):
        end += 1
    return [_OPTION.match(rows[index]).group("text").strip() for index in range(start, end + 1)
            if rows[index].strip() and _label_column(rows[index]) == column]


def _numbered_at(row: str, column: int) -> bool:
    match = _OPTION.match(row)
    return bool(match and match.group("number")) and _label_column(row) == column


def _label_column(row: str) -> int:
    """Where an option row's label starts: after any cursor glyph, at its number if it has one."""
    match = _OPTION.match(row)
    if match is None:
        return -1
    return match.start("number") if match.group("number") else match.start("text")


def _is_footer(row: str) -> bool:
    return bool(_ENTER.search(row) and _HINT.search(row)) and not _OPTION.match(row).group("cursor")
