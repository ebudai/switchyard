"""The provider's sign-in link, printed by Switchyard as one line the User clicks in their own terminal (SYRD-782).

A provider CLI runs its sign-in as the project's agent account. Left to itself it opens a browser as that account, and
on the User's desktop that browser cannot come to the front: KWin refuses an activation token to a client the User has
not touched, so the window maps behind the terminal, and it closes with the sign-in step. Measured on test11, whose
Firefox ran for 46 seconds behind the User's terminal and was never seen.

So for the sign-in, the agent opens nothing (`BROWSER=true`, which claude, codex and copilot honour, and a no-op
`xdg-open` first on PATH for agy, which calls it directly), and Switchyard prints the URL the CLI shows as a single
unbroken line, wrapped in an OSC 8 hyperlink. The User clicks it in their own terminal, which has their input, so the
page opens in front, in their own browser. That is the User's own act: nothing of the agent's touches the User's
browser or profile.

- **One line.** A URL broken over several rows is several URLs to a terminal's own link detection. Claude's
  full-screen first run draws it that way, which is why Ctrl+click opened only the first row (SYRD-780). This line is
  never broken: the terminal soft-wraps it, so the whole URL is detected, and OSC 8 covers every row where the
  terminal honours it.
- **Found, not assumed.** The URL is read from what the CLI prints: an OSC 8 target first, which survives any wrapping;
  in line mode, also a plain `https://` URL once the line that holds it has ended. Only `https://` URLs made of URL
  characters are printed, so nothing the agent's process prints can put a control sequence into this line.
- **Where it goes.** In line mode (claude auth login, codex login, copilot login) it follows the line that held the
  URL, before the CLI's next prompt. In a full-screen sign-in on the alternate screen (agy) the CLI's terminal is made
  shorter and the link is drawn in the rows that frees at the bottom, where the CLI never draws, and drawn again
  whenever the CLI clears its screen.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
from typing import Any, Callable

#: The agent opens no browser of its own during sign-in: claude, codex and copilot run `$BROWSER <url>`, and `true`
#: opens nothing. Claude treats exactly "true" as "no browser of the user's choosing", so its own headless rules still
#: decide which sign-in flow it offers.
BROWSER_VALUE = "true"
#: The program agy runs from PATH, whatever BROWSER says (measured, agy 1.5.1).
SHADOWED_OPENERS = ("xdg-open",)

#: An OSC 8 hyperlink's target, ended by BEL or ST.
_OSC8_TARGET = re.compile(r"\x1b\]8;[^;\x07\x1b]*;(https://[^\x07\x1b]+)(?:\x07|\x1b\\)")
#: Escape sequences, removed to read plain text.
_ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
#: URL characters (RFC 3986), and nothing else.
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
_PLAIN_URL = re.compile(rf"https://[{_URL_CHARS}]+(?=[\s\x1b])")
_WHOLE_URL = re.compile(rf"https://[A-Za-z0-9\-.]+(?::\d+)?(?:/[{_URL_CHARS}]*)?")
_ALT_SCREEN = re.compile(r"\x1b\[\?(?:1049|1047|47)([hl])")
#: A screen clear, after which a link drawn in the reserved rows has to be drawn again.
_CLEARS = re.compile(r"\x1b\[[0-3]?J|\x1b\[\?(?:1049|1047|47)h|\x1bc")
MAX_URL = 4096
#: How much of a step's output is kept to find the URL in.
SEEN_LIMIT = 65536


def printable_url(url: str) -> str:
    """`url` if it is an https URL of URL characters alone, of a sane length; otherwise ""."""
    return url if len(url) <= MAX_URL and _WHOLE_URL.fullmatch(url) else ""


def link(url: str) -> str:
    """The URL as one OSC 8 hyperlink whose text is the URL itself: clickable where OSC 8 is honoured, and found by the
    terminal's own link detection where it is not, because it is never broken."""
    return f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"


def instruction(cli: str, owner_user: str) -> str:
    return (f"switchyard: To sign in {owner_user} to {cli}, open this link in your own browser (click it, or "
            "Ctrl+click), then paste back here any code the page shows:")


def _ends_cleanly(text: str) -> bool:
    """Whether `text` ends outside an escape sequence, so bytes of our own can follow it. A string sequence (OSC, DCS,
    APC, PM, SOS) is complete only at its BEL or ST; a CSI only at its final byte (a replay of agy's sign-in in a real
    terminal showed the link written between agy's `ESC ]` and the rest of its hyperlink)."""
    at = text.rfind("\x1b")
    if at == -1:
        return True
    rest = text[at + 1:]
    if not rest:
        return False
    if rest[0] in "]P_^X":
        return "\x07" in rest  # its ST would be a later ESC, so only a BEL can have ended it
    if rest[0] == "[":
        return re.fullmatch(r"\[[0-?]*[ -/]*[@-~].*", rest, re.S) is not None
    return True


class SignInLink:
    """Watches one sign-in step's output and says, once, where the link goes and what to write.

    `feed(chunk)` returns the pieces to write in order: the CLI's own output, unchanged, with Switchyard's text
    between, tagged so the caller can keep its own text out of what it reads of the CLI's screen.
    """

    def __init__(self, *, cli: str, owner_user: str) -> None:
        self.cli = cli
        self.owner_user = owner_user
        self.url = ""
        self._seen = ""
        self._alternate = False
        self._drawn_at: tuple[int, int] | None = None  # (rows, cols) the reserved rows were drawn for
        self.reserved = 0
        #: Found on the alternate screen, waiting for a read that ends between two escape sequences.
        self._pending = False
        #: The CLI cleared its screen since the link was last drawn: drawn again at the next clean boundary.
        self._dirty = False

    # -- what to write --------------------------------------------------------------------------------------------
    def block(self) -> str:
        """Line mode: the instruction and the link, each on its own line."""
        return f"\r\n{instruction(self.cli, self.owner_user)}\r\n{link(self.url)}\r\n\r\n"

    def rows_needed(self, cols: int) -> int:
        cols = max(cols, 20)
        return 1 + -(-len(instruction(self.cli, self.owner_user)) // cols) + -(-len(self.url) // cols)

    def reserved_block(self, rows: int, cols: int) -> str:
        """Alternate screen: the same text, drawn in the bottom `self.reserved` rows, cursor kept. The rows above are
        made the scroll region too: a CLI whose frame is taller than its now shorter terminal moves down by newlines,
        and without it those reach these rows (measured, agy 1.5.1)."""
        top = rows - self.reserved + 1
        clear = "".join(f"\x1b[{row};1H\x1b[2K" for row in range(top, rows + 1))
        return (f"\x1b7\x1b[1;{top - 1}r{clear}\x1b[{top + 1};1H\x1b[0m{instruction(self.cli, self.owner_user)}\r\n"
                f"{link(self.url)}\x1b8")

    def release(self) -> str:
        """What gives the person's terminal its whole height back as the scroll region, once the step is over."""
        return "\x1b7\x1b[r\x1b8" if self.reserved else ""

    # -- reading the CLI's output ---------------------------------------------------------------------------------
    def feed(self, chunk: str, *, size: tuple[int, int] | None = None,
             reserve: Callable[[int], bool] | None = None) -> list[tuple[str, bool]]:
        """[(text, is_ours)], in order. `size` is the outer terminal's (rows, cols); `reserve(n)` makes the CLI's
        terminal `n` rows shorter and says whether it could."""
        for match in _ALT_SCREEN.finditer(chunk):
            self._alternate = match.group(1) == "h"
        if self.url:
            if self._pending:
                return [(chunk, False), *self._reserve(chunk, size, reserve)]
            if self.reserved and _CLEARS.search(chunk):
                self._dirty = True
            if self.reserved and size and (self._dirty or self._drawn_at != size) and _ends_cleanly(chunk):
                need = self.rows_needed(size[1])
                if self._drawn_at != size and need != self.reserved and need < size[0] // 2 and reserve is not None \
                        and reserve(need):
                    self.reserved = need  # a narrower window needs more rows for the same URL
                self._dirty = False
                self._drawn_at = size
                return [(chunk, False), (self.reserved_block(*size), True)]
            return [(chunk, False)]
        start = len(self._seen)
        self._seen += chunk
        if len(self._seen) > SEEN_LIMIT:
            start -= len(self._seen) - SEEN_LIMIT
            self._seen = self._seen[-SEEN_LIMIT:]
        url, end = self._find()
        if not url:
            return [(chunk, False)]
        self.url = url
        self._seen = ""
        if self._alternate:
            self._pending = True
            return [(chunk, False), *self._reserve(chunk, size, reserve)]
        split = max(0, min(len(chunk), end - start))
        return [(chunk[:split], False), (self.block(), True), (chunk[split:], False)]

    def _reserve(self, chunk: str, size: tuple[int, int] | None,
                 reserve: Callable[[int], bool] | None) -> list[tuple[str, bool]]:
        """The link in rows kept from the CLI, once `chunk` ends between escape sequences; [] until then, and for
        good when there is nowhere to keep them (it is said after the CLI's screen is gone)."""
        if not _ends_cleanly(chunk):
            return []
        self._pending = False
        if size is None or reserve is None:
            return []
        need = self.rows_needed(size[1])
        if need >= size[0] // 2 or not reserve(need):
            return []
        self.reserved = need
        self._drawn_at = size
        return [(self.reserved_block(*size), True)]

    def _find(self) -> tuple[str, int]:
        """(the URL, where in the seen text the line holding it ends), or ("", 0) until both are known."""
        text = self._seen
        for match in _OSC8_TARGET.finditer(text):
            url = printable_url(match.group(1))
            if url:
                if self._alternate:
                    return url, match.end()
                newline = text.find("\n", match.end())
                if newline != -1:
                    return url, newline + 1
                return "", 0
        if self._alternate:
            return "", 0  # a full-screen CLI breaks a plain URL across rows: only an OSC 8 target is whole
        plain = _ESCAPES.sub("", text)
        for match in _PLAIN_URL.finditer(plain):
            url = printable_url(match.group(0))
            if not url:
                continue
            raw = text.find(url)
            if raw == -1:
                continue  # not printed as one run of characters: nothing glued from either side of an escape
            newline = text.find("\n", raw + len(url))
            if newline != -1:
                return url, newline + 1
            return "", 0
        return "", 0

    def leftover(self) -> str:
        """Said once the CLI's screen is gone, for a link that had nowhere safe to go while it ran."""
        if self.url and not self.reserved and self._alternate:
            return f"{instruction(self.cli, self.owner_user)}\n{link(self.url)}"
        return ""


_OPENERS_DIR: list[str] = []


def no_browser_dir() -> str:
    """A directory holding a no-op `xdg-open`, made once per process by whoever runs Switchyard -- root, for a tenant
    -- so the agent can run it and no other account can change it. Removed when the process ends."""
    if _OPENERS_DIR and os.path.isdir(_OPENERS_DIR[0]):
        return _OPENERS_DIR[0]
    path = tempfile.mkdtemp(prefix="switchyard-sign-in-")
    os.chmod(path, 0o755)
    for name in SHADOWED_OPENERS:
        target = os.path.join(path, name)
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("#!/bin/sh\n# Switchyard sign-in: the agent opens no browser; the User clicks the link.\nexit 0\n")
        os.chmod(target, 0o755)
    _OPENERS_DIR[:] = [path]
    atexit.register(shutil.rmtree, path, True)
    return path


def no_browser_transform(args: list[str], kwargs: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The owner command, with the agent's own browser launch turned off: `BROWSER=true`, and the no-op opener
    directory first on its PATH. Added beside the command's own `PATH=`, so an `env` option inserted after `env` by
    another transform still comes before every assignment."""
    args = list(args)
    if "env" not in args:
        return args, kwargs
    start = args.index("env") + 1
    at = next((i for i in range(start, len(args)) if args[i].startswith("PATH=")), None)
    directory = no_browser_dir()
    if at is None:
        at = next((i for i in range(start, len(args)) if "=" in args[i] and not args[i].startswith("-")), start)
        args[at:at] = [f"BROWSER={BROWSER_VALUE}", f"PATH={directory}:/usr/local/bin:/usr/bin:/bin"]
    else:
        args[at:at + 1] = [f"BROWSER={BROWSER_VALUE}", f"PATH={directory}:{args[at][len('PATH='):]}"]
    return args, kwargs
