"""Running a provider CLI's interactive first run in the foreground, and owning the terminal while it does.

A provider's first run (sign-in, folder trust) has to be answered by a person,
so Switchyard runs the CLI on a pty in the setup window and proxies it:
- `run_provider_first_run_session` and `_run_provider_first_run` build the
  owner command and drive one step to its end;
- `PtyForegroundSession` spawns the child on a pty, forwards input, watches
  the screen and decides when the step is finished -- never while the
  provider is asking something;
- `_RawTerminal` and `_TerminalModeLedger` hold the terminal raw until the
  child is gone and hand back every mode the provider set;
- `_own_the_terminal`, `_terminal_window_size`, `_set_terminal_window_size`
  and `set_terminal_title` own the terminal's size and title;
- `_SetupWindowNarrator` and `_countdown` tell the person what is happening;
- the `SETUP_*`, `PROVIDER_*` and `FOREGROUND_*` constants are its wording and
  timings.

Reading what a screen shows is `scripts/provider_screen.py`, a pure leaf this
module imports; it never imports this one. The launcher helpers that build the
owner's environment (`_owner_command_env_args`, `_pane_identity_scrubbed_env`)
are read from `scripts.team_launcher` at call time, so patches on the launcher
still reach them. Deciding which providers need a first run, and reporting the
result, is the auth phase, `scripts/first_run_auth.py` (SYRD-300).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-299). `team_launcher`
imports this module at its top and still exports every name callers read there.
"""

from __future__ import annotations

import os
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from scripts.provider_screen import (
    _draws_something,
    _replaced_frame_starts_at,
    _screen_is_settled,
    _TerminalStream,
)


#: How often a bounded foreground step looks to see whether the thing it was
#: opened for has been recorded, and how long it waits in total. The CLI keeps
#: running after it commits the state -- it goes on to its normal prompt -- so
#: something has to notice and hand the terminal back (SYRD-191).
FOREGROUND_COMPLETION_POLL_SECONDS = 0.5


#: Long enough for a person to answer a prompt, including an OAuth round trip in
#: a browser; short enough that a step nothing will ever record ends in minutes
#: with an explanation rather than looking like a hang. Live on the testing
#: tenant it was the second case: a trust step was scheduled for a worktree
#: Claude already trusted, so it opened at a ready prompt and the watcher waited
#: for a key that was never going to be written (SYRD-191).
FOREGROUND_COMPLETION_TIMEOUT_SECONDS = 600.0


#: A gap long enough to mean the provider has finished drawing one screen and
#: started another. Redraws arrive as a burst of chunks; anything after a pause
#: this long is a new screen, and what was on the last one stops counting --
#: otherwise an OAuth box printed a minute ago keeps reading as a live question
#: forever.
PROVIDER_SCREEN_RESET_SECONDS = 1.0


#: How long a provider must be BOTH silent and not waiting on anybody before its
#: first run counts as finished. It is not a deadline -- a step that is still
#: asking something resets it, however long the person takes.
PROVIDER_READY_QUIET_SECONDS = 6.0


#: What ends a provider's own session once its first run is done. Sent BY
#: Switchyard into the session it started, so the person never types it: being
#: asked to exit once for the account and again for every worktree is the chore
#: this whole phase exists to avoid.
PROVIDER_SESSION_EXIT_INPUT: dict[str, str] = {"claude": "/exit\r"}


#: What the window is called while a temporary setup step owns the terminal,
#: and which of them it is: a fresh tenant meets one of these per provider for
#: the first run, per provider for a sign-in, and one PER WORKTREE for folder
#: trust. Naming only the first is how a User counting windows concludes the
#: launch stopped in a standalone CLI (SYRD-221).
#:
#: Measured rather than hoped: Claude sets no title of its own while its setup
#: screens are up, and it CLEARS the title when it starts -- an empty `OSC 0`.
#: So a name set once before the CLI starts does not survive it; only the
#: republished one below does.
SETUP_WINDOW_TITLE = "Switchyard setup (temporary): {cli} {purpose}"


#: The steps a person can be sat in front of, worded as the thing they are
#: being asked to finish rather than as the function that runs it.
SETUP_PURPOSE_FIRST_RUN = "first run"


SETUP_PURPOSE_SIGN_IN = "sign-in"


SETUP_PURPOSE_FOLDER_TRUST = "folder trust"


#: What did not happen, per step, so giving up names the answer that is still
#: outstanding instead of only the clock. Keyed by the purpose above.
SETUP_STEP_DONE_AT_PROMPT = {
    SETUP_PURPOSE_FIRST_RUN: (
        "{cli} has finished its first run and is at its ordinary prompt; closing it and "
        "carrying on. You do not have to exit anything."
    ),
    SETUP_PURPOSE_SIGN_IN: (
        "{cli} is signed in and at its ordinary prompt; closing it and carrying on. You "
        "do not have to exit anything."
    ),
    SETUP_PURPOSE_FOLDER_TRUST: (
        "{cli} is at its ordinary prompt with nothing left to ask about this directory, "
        "so this step is done; closing it and carrying on. You do not have to exit "
        "anything."
    ),
}


SETUP_STEP_STALLED_DETAIL = {
    SETUP_PURPOSE_FIRST_RUN: (
        "{cli} did not record its first run, so it was still asking for something when "
        "time ran out -- most often the sign-in that follows the theme question."
    ),
    SETUP_PURPOSE_SIGN_IN: (
        "{cli} did not record a signed-in account, so the sign-in was still unfinished "
        "when time ran out -- most often an authorization code that was never pasted back."
    ),
    SETUP_PURPOSE_FOLDER_TRUST: (
        "{cli} did not record trust for that directory, so it was still asking whether "
        "to trust it when time ran out."
    ),
}


#: What the window says it is waiting for while the provider owns the screen.
#: The title is the only channel available: this CLI draws inline rather than
#: on the alternate screen, so anything written to stdout lands in the middle
#: of what it is drawing (SYRD-221).
SETUP_WINDOW_WAITING_TITLE = (
    "Switchyard setup: answer {cli}'s prompts to the end -- {remaining} left"
)


#: How often that countdown is refreshed. Often enough that a person glancing
#: at the title learns something, rarely enough to be no part of the drawing.
SETUP_WINDOW_TITLE_INTERVAL_SECONDS = 5.0


#: And what it goes back to afterwards, so a finished setup window does not
#: keep claiming to be one.
SETUP_WINDOW_TITLE_DONE = "Switchyard"


#: Cursor home, then erase the display. Not `ESC c`, which would reset modes
#: and scrollback the person may want.
SETUP_STEP_CLEAR_SCREEN = "\x1b[H\x1b[2J"


def set_terminal_title(text: str, *, write: Callable[[str], Any] | None = None) -> None:
    """Name the terminal window, when there is one to name.

    Silent on anything that is not a terminal: this is decoration for a person
    watching, and writing escape bytes into a captured stream would corrupt
    output that something else is parsing.
    """
    if write is None:
        stream = sys.stdout
        if not stream or not hasattr(stream, "isatty") or not stream.isatty():
            return
        write = stream.write
    write(f"\033]0;{text}\a")


def _terminal_window_size(fd: int) -> tuple[int, int] | None:
    """The rows and columns of a real terminal, or None if it is not one."""
    import fcntl
    import termios

    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except (OSError, ValueError):
        return None
    rows, cols, _xpix, _ypix = struct.unpack("HHHH", packed)
    if not rows or not cols:
        return None
    return rows, cols


def _set_terminal_window_size(fd: int, size: tuple[int, int]) -> None:
    import fcntl
    import termios

    rows, cols = size
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (OSError, ValueError):
        pass


def _own_the_terminal() -> None:
    """Make the provider a session leader owning the pty it was handed.

    Run in the child between fork and exec. Without it the pty has no session
    and no foreground process group -- `tcgetpgrp` fails with ENOTTY -- so a
    window-size change signals nobody, and the provider goes on drawing to the
    size it started at however the window is dragged. Measured on Zorin against
    the real CLI: started at 118 columns, terminal reduced to 62, and it kept
    drawing 116-column rules (SYRD-221).

    It matters most for the shape that actually ships. A provider is launched
    across a user boundary through `sudo -u`, which puts it in a session of its
    own, so it cannot pick up the outer terminal's signals by inheritance the
    way a same-session child does.
    """
    import fcntl
    import termios

    try:
        os.setsid()
    except OSError:
        # Already a session leader, which is the state this wanted anyway --
        # a caller may have asked for `start_new_session`, and failing here
        # would kill the launch between fork and exec for no reason.
        pass
    try:
        # Descriptor 0 is the pty slave here: it was handed to this child as
        # its standard input and the parent has not closed it yet.
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        # A kernel that will not hand it over is not a reason to abandon the
        # run; the window simply will not follow a resize.
        pass


#: Private modes a provider switches on in the OUTER terminal by printing
#: through the proxy, and the state each one has to be left in. Every one of
#: these is in a recording of the real CLI (tests/fixtures/claude-first-run).
#: Left on after the provider is gone, they belong to nobody: focus reporting
#: makes the terminal type `^[[I` / `^[[O` into the person's shell every time
#: they switch windows, and mouse reporting does the same for every click --
#: the raw `^[[...` test8 showed after its sign-in was closed (SYRD-221 UAT).
TERMINAL_PRIVATE_MODES_AT_REST: dict[str, str] = {
    "1000": "l",  # mouse: clicks
    "1002": "l",  # mouse: drags
    "1003": "l",  # mouse: all motion
    "1006": "l",  # mouse: SGR encoding
    "1004": "l",  # focus in/out reporting
    "2004": "l",  # bracketed paste
    "2031": "l",  # colour-scheme change notifications
    "1049": "l",  # alternate screen
    "25": "h",    # cursor visible
}


_PRIVATE_MODE = re.compile(r"\x1b\[\?([0-9;]+)([hl])")


_KITTY_KEYBOARD = re.compile(r"\x1b\[([<>])([0-9]*)u")


_MODIFY_OTHER_KEYS = re.compile(r"\x1b\[>4(?:;([0-9]*))?m")


class _TerminalModeLedger:
    """What a provider has switched on in the outer terminal, so it can be undone.

    The proxy passes every byte through, which is what makes it faithful -- and
    also means a provider's `ESC[?1004h` reaches the person's own terminal, not
    only its pty. A provider that exits cleanly switches its modes off again on
    the way out. One that is ended by Switchyard, or killed on a deadline, does
    not, and the terminal is then handed back still configured for an
    application that no longer exists.

    Read from the output itself rather than assumed, so only what was actually
    turned on is turned off: writing `ESC[?1049l` to a terminal that never left
    its main screen restores a saved cursor position nobody saved.
    """

    def __init__(self) -> None:
        self._modes: dict[str, str] = {}
        self._kitty_pushes = 0
        self._modify_other_keys = False
        # An escape split across two reads is the normal case for a stream, not
        # an edge.
        self._stream = _TerminalStream()

    def feed(self, chunk: str) -> None:
        text = self._stream.feed(chunk)
        for match in _PRIVATE_MODE.finditer(text):
            for mode in match.group(1).split(";"):
                if mode in TERMINAL_PRIVATE_MODES_AT_REST:
                    self._modes[mode] = match.group(2)
        for match in _KITTY_KEYBOARD.finditer(text):
            count = int(match.group(2) or 0)
            if match.group(1) == ">":
                self._kitty_pushes += 1
            else:
                self._kitty_pushes = max(0, self._kitty_pushes - max(count, 1))
        for match in _MODIFY_OTHER_KEYS.finditer(text):
            self._modify_other_keys = bool(int(match.group(1) or 0))

    def handback(self) -> str:
        """The bytes that put back everything still switched on. Empty if nothing is."""
        parts = []
        for mode, rest in TERMINAL_PRIVATE_MODES_AT_REST.items():
            state = self._modes.get(mode)
            if state is not None and state != rest:
                parts.append(f"\x1b[?{mode}{rest}")
        if self._kitty_pushes:
            parts.append(f"\x1b[<{self._kitty_pushes}u")
        if self._modify_other_keys:
            parts.append("\x1b[>4m")
        return "".join(parts)


class _RawTerminal:
    """The outer terminal, made byte-transparent while a provider owns it.

    A pty proxy that leaves the outer terminal in canonical mode with echo on
    is not a proxy, it is a second voice on the same screen. Measured against
    the real CLI: Claude asks the terminal questions -- primary device
    attributes, the kitty keyboard protocol -- and the terminal answers on this
    process's stdin. The line discipline then ECHOES those answers, so
    `^[[?62;1;4c` and `^[[?0u` are drawn into the middle of the theme menu.
    Canonical mode also holds each keystroke until Enter, so the arrow keys
    that menu is driven with never arrive at all (SYRD-221).

    Raw mode is what makes the relay faithful: nothing echoed, nothing
    buffered, every byte passed through exactly once. Restored afterwards
    whatever happens, because leaving somebody's terminal in raw mode is worse
    than anything this was fixing.
    """

    #: The ways this process can be killed while it is holding somebody's
    #: terminal raw. Python's cleanup does not run for any of them, so without
    #: a handler the terminal is left with `icanon=False echo=False
    #: isig=False` -- measured after a SIGTERM -- and the person's shell is
    #: unusable with no indication why (SYRD-221). SIGKILL cannot be caught and
    #: is the one case nothing here can help with.
    FATAL_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)

    def __init__(self, fd: int | None) -> None:
        self._fd = fd
        self._saved: Any = None
        self._handlers: dict[int, Any] = {}

    def __enter__(self) -> "_RawTerminal":
        import termios
        import tty

        if self._fd is None:
            return self
        try:
            if not os.isatty(self._fd):
                return self
            self._saved = termios.tcgetattr(self._fd)
            tty.setraw(self._fd, termios.TCSANOW)
        except (termios.error, OSError, ValueError):
            self._saved = None
            return self
        self._catch_fatal_signals()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.restore()
        return False

    @property
    def engaged(self) -> bool:
        """Is a real terminal being held raw right now?"""
        return self._saved is not None

    def _catch_fatal_signals(self) -> None:
        for number in self.FATAL_SIGNALS:
            try:
                self._handlers[number] = signal.signal(number, self._dying)
            except (ValueError, OSError, RuntimeError):
                # Not the main thread, or a platform without it. The terminal
                # is still restored on every path Python itself unwinds.
                self._handlers.pop(number, None)

    def _release_fatal_signals(self) -> None:
        while self._handlers:
            number, previous = self._handlers.popitem()
            try:
                signal.signal(number, previous)
            except (ValueError, OSError, RuntimeError):
                pass

    def _dying(self, number: int, _frame: Any) -> None:
        """Hand the terminal back, then die the way we were told to.

        Re-raised rather than swallowed: a signal that says stop must still
        stop this, and the caller's own disposition decides what that means.
        """
        self.restore()
        os.kill(os.getpid(), number)

    def restore(self) -> None:
        """Give the terminal back. Safe to call more than once."""
        import termios

        self._release_fatal_signals()
        if self._saved is None:
            return
        saved, self._saved = self._saved, None
        try:
            # FLUSH, not DRAIN: whatever the terminal typed at the provider and
            # nobody has read yet -- an answer to its last query, a focus event
            # -- would otherwise be read by the person's shell in cooked mode
            # and echoed as `^[[...` (SYRD-221 UAT).
            termios.tcsetattr(self._fd, termios.TCSAFLUSH, saved)
        except (termios.error, OSError, ValueError):
            pass


class PtyForegroundSession:
    """One provider run on a real terminal, watched while the person uses it.

    A pipe is not good enough here. These CLIs draw a full-screen interface and
    behave differently without a terminal, and the step has to be able to both
    hand the person their keyboard and see what the provider is putting on the
    screen. A pty gives it both: the child believes it owns a terminal, and this
    side reads every byte it writes (SYRD-211 live UAT).
    """

    def __init__(self, args: Sequence[str], **kwargs: Any) -> None:
        import pty

        import codecs

        #: Holds a character that arrives split across two reads.
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._master, slave = pty.openpty()
        os.set_blocking(self._master, False)
        # A pty opened cold is 80x24 whatever the window is, so a full-screen
        # CLI lays itself out for a terminal nobody is looking at. Measured on
        # the theme menu: the wrong width wraps the option list into the
        # preview below it (SYRD-221).
        self._size = self._outer_size()
        if self._size is not None:
            _set_terminal_window_size(slave, self._size)
        try:
            self._process = subprocess.Popen(
                list(args),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=_own_the_terminal,
                **kwargs,
            )
        finally:
            os.close(slave)

    @staticmethod
    def _outer_size() -> tuple[int, int] | None:
        for stream in (sys.stdout, sys.stdin):
            try:
                fd = stream.fileno()
            except (AttributeError, ValueError, OSError):
                continue
            size = _terminal_window_size(fd)
            if size is not None:
                return size
        return None

    def sync_window_size(self) -> None:
        """Follow the window if somebody resizes it mid-setup."""
        size = self._outer_size()
        if size is None or size == self._size:
            return
        self._size = size
        _set_terminal_window_size(self._master, size)

    def wait_for_activity(self, timeout: float, input_fd: int | None = None) -> None:
        """Sleep until there is something to do, rather than for a fixed time.

        A blind poll costs a person two waits per keystroke: up to one interval
        before what they typed is passed on, and up to another before the
        redraw it caused is noticed. Measured at 0.80-1.00s round trip on a
        0.5s interval, which is what an interactive menu feels like when every
        arrow key lags a second behind the finger (SYRD-221).

        The timeout still bounds it, because the loop has work of its own that
        no descriptor will wake it for: the countdown, the deadline, and the
        quiet window that decides the provider has finished.
        """
        watching = [self._master]
        if input_fd is not None:
            try:
                if os.isatty(input_fd):
                    watching.append(input_fd)
            except (OSError, ValueError):
                pass
        try:
            select.select(watching, [], [], timeout)
        except (OSError, ValueError, InterruptedError):
            time.sleep(timeout)

    def read(self) -> str:
        try:
            chunk = os.read(self._master, 65536)
        except (BlockingIOError, InterruptedError):
            return ""
        except OSError:
            return ""
        # Decoded across reads, not within one. A read lands wherever the
        # kernel has bytes, and a character the provider drew in three of them
        # -- the prompt glyph is one -- arrives split. Decoding each read on
        # its own turns that into replacement characters on the screen, which
        # is the malformed boundary the live run reported (SYRD-221).
        return self._decoder.decode(chunk)

    def write(self, text: str) -> None:
        try:
            os.write(self._master, text.encode("utf-8"))
        except OSError:
            pass

    def relay_from(self, source_fd: int) -> None:
        """Whatever the person types goes to the provider, unchanged.

        Asked whether there IS anything first, with a zero timeout. Reading a
        terminal straight away blocks until somebody types, and this runs inside
        the loop that watches the provider: on any real tty the watch stopped
        dead at the first turn, so the screen was never read, the quiet window
        never advanced, and a session at its ordinary prompt was never closed --
        the very failure the loop exists to end (SYRD-211 Audit kick-back).
        """
        try:
            ready, _, _ = select.select([source_fd], [], [], 0)
        except (OSError, ValueError):
            return
        if not ready:
            return
        try:
            data = os.read(source_fd, 65536)
        except (BlockingIOError, InterruptedError, OSError):
            return
        if data:
            try:
                os.write(self._master, data)
            except OSError:
                pass

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def close(self) -> None:
        try:
            os.close(self._master)
        except OSError:
            pass


def _run_provider_first_run(
    *,
    cli: str,
    owner_user: str,
    owner_home: Path,
    command: Sequence[str],
    is_complete: Callable[[], bool],
    watching: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    transform: Callable[[list[str], dict[str, Any]], tuple[list[str], dict[str, Any]]] | None = None,
    session_factory: Callable[..., Any] | None = None,
    print_func: Callable[[str], None] = print,
    **watch_kwargs: Any,
) -> bool:
    """The provider's own first run: driven by a caller, or watched on a pty.

    An injected runner means a suite is driving the step, and it keeps the old
    shape -- run it, then read the account back. Live, the step gets a real
    terminal and Switchyard ends it itself (SYRD-211 live UAT).
    """
    from scripts import team_launcher as launcher

    args = launcher._owner_command_env_args(owner_user, owner_home, command)
    kwargs: dict[str, Any] = {"cwd": str(owner_home), "env": launcher._pane_identity_scrubbed_env()}
    if transform is not None:
        args, kwargs = transform(args, kwargs)
    if runner is not None:
        runner(args, **kwargs)
        return is_complete()
    if is_complete():
        return True
    return run_provider_first_run_session(
        cli=cli,
        args=args,
        kwargs=kwargs,
        is_complete=is_complete,
        watching=watching,
        session_factory=session_factory,
        input_fd=sys.stdin.fileno() if sys.stdin and sys.stdin.isatty() else None,
        print_func=print_func,
        **watch_kwargs,
    )


def _countdown(seconds: float) -> str:
    """A remaining time somebody can read at a glance."""
    whole = max(0, int(seconds))
    if whole >= 60:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole}s"


class _SetupWindowNarrator:
    """Keep a temporary setup window saying what it is for, and that it is live.

    Every foreground provider step has the same problem: the CLI owns the
    screen and redraws it, so anything Switchyard prints is scribbled over,
    and a step that is waiting for a person looks exactly like one that has
    hung. The title is the one channel the CLI does not contend for -- and it
    has to be republished, because Claude clears it on the way in.

    The countdown is the part that cannot be faked by a static name: it says
    the wait is still running and how much of it is left. A window that is
    counting down is not a window that has stopped.
    """

    def __init__(
        self,
        *,
        cli: str,
        purpose: str,
        deadline: float,
        monotonic: Callable[[], float] = time.monotonic,
        write: Callable[[str], Any] | None = None,
        interval: float = SETUP_WINDOW_TITLE_INTERVAL_SECONDS,
    ) -> None:
        self._cli = cli
        self._purpose = purpose
        self._deadline = deadline
        self._monotonic = monotonic
        self._write = write
        self._interval = interval
        self._last = monotonic()

    def opened(self) -> None:
        """Name the window before the CLI takes it."""
        set_terminal_title(
            SETUP_WINDOW_TITLE.format(cli=self._cli, purpose=self._purpose),
            write=self._write,
        )

    def tick(self) -> None:
        """Republish the name and the time left, if it is due."""
        now = self._monotonic()
        if now - self._last < self._interval:
            return
        set_terminal_title(
            SETUP_WINDOW_WAITING_TITLE.format(
                cli=self._cli, remaining=_countdown(self._deadline - now)
            ),
            write=self._write,
        )
        self._last = now

    def done_at_prompt(self) -> str:
        """What to say when the screen, not the state file, is what finished it.

        A step can be over without the CLI recording anything Switchyard reads:
        the person answered, the provider went back to its ordinary prompt, and
        nothing more is coming. Saying "finished its first run" for a folder
        trust step would be a guess dressed as a fact, so each step says what
        it actually observed.
        """
        return "switchyard: " + SETUP_STEP_DONE_AT_PROMPT.get(
            self._purpose,
            "{cli} is at its ordinary prompt, so this step is done; closing it and "
            "carrying on. You do not have to exit anything.",
        ).format(cli=self._cli)

    def at_prompt_but_unrecorded(self) -> str:
        """The screen said finished and the account does not agree.

        Said instead of `done_at_prompt`, never as well as it. The two used to
        be decided at different moments -- the message while the provider was
        still running, the result after it had gone -- so a step announced
        itself done at an ordinary prompt and then blocked the launch as
        unauthenticated. Live on sbs: "agy is at its ordinary prompt with
        nothing left to ask, so this step is done", and then "agy: audit
        (sign-in not finished)". Both cannot be true, and the account is the
        one that decides (SYRD-221).
        """
        return (
            f"switchyard: {self._cli} went back to its ordinary prompt, but its "
            f"{self._purpose} is still not recorded for this account, so no role using "
            f"{self._cli} can start yet. Nothing was lost: finish it by running {self._cli} "
            "as that account, then run the same switchyard command again -- it picks up "
            "from whatever is already recorded."
        )

    def could_not_start(self, *, args: Sequence[str], cwd: object, exc: BaseException) -> str:
        """What to say when the provider never ran at all.

        The gap this fills is not a wrong message, it is no message. A fresh
        `test15` was told "claude will now run in .../audit ... Answer the trust
        prompt", and then nothing: no provider process, no prompt, a cursor on a
        window still titled "Switchyard". The step had taken the terminal, the
        spawn had failed, and the failure went out as a bare exception with
        nothing naming which worktree or which CLI it was (SYRD-245).

        Says the three things an operator needs to act: which step, what was
        actually run, and what the system said about it.
        """
        where = f" in {cwd}" if cwd else ""
        return (
            f"warning: switchyard: {self._cli} could not be started{where} for its "
            f"{self._purpose}, so that step recorded nothing: {exc}. Nothing was "
            f"asked of you and nothing is waiting. The command was: "
            f"{shlex.join(str(part) for part in args)}. The step and how to resume "
            "it are reported below; run that command yourself to see what it says."
        )

    def stalled(self, *, watching: str, timeout_seconds: float) -> str:
        """What to say when the deadline passed with the answer still missing."""
        detail = SETUP_STEP_STALLED_DETAIL.get(
            self._purpose, "{cli} did not record what this step waits for."
        ).format(cli=self._cli)
        return (
            f"warning: switchyard: gave up waiting {timeout_seconds:g}s for {watching}. "
            f"{detail} The CLI was ended and the run continues; the step and how to "
            "resume it are reported below. If the CLI showed no prompt at all, it "
            "already considers this done and Switchyard is reading the wrong state -- "
            "say so, because that is a defect here and not something to answer again."
        )

    def closed(self) -> None:
        """Give the name back, so a finished window stops claiming to be setup."""
        set_terminal_title(SETUP_WINDOW_TITLE_DONE, write=self._write)


def run_provider_first_run_session(
    *,
    cli: str,
    args: Sequence[str],
    kwargs: dict[str, Any],
    is_complete: Callable[[], bool],
    watching: str,
    session_factory: Callable[..., Any] | None = None,
    input_fd: int | None = None,
    output_write: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    quiet_seconds: float = PROVIDER_READY_QUIET_SECONDS,
    timeout_seconds: float = FOREGROUND_COMPLETION_TIMEOUT_SECONDS,
    purpose: str = SETUP_PURPOSE_FIRST_RUN,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Run a provider's own first run and end it as soon as it is finished.

    Finished means one of two OBSERVED things, never a bare timer:

    * the account records its first run -- the fast path, when the provider
      writes that while it is still running; or
    * the provider stops printing and its last screen is not asking anybody
      anything. That is the ordinary prompt, and it is where live Zorin UAT
      stopped: the questions were answered, Claude sat at its prompt, and
      Switchyard waited ten minutes for a key that is not written until exit.

    Either way Switchyard ends the session itself -- by sending the provider's
    own exit input, then terminating if it does not go. The person types
    nothing, which is the whole point: `/exit` once for the account and again
    for every worktree is the chore this phase exists to remove.

    A screen that is still asking something resets the quiet window, so somebody
    reading an OAuth code box or a theme list is never cut off however long they
    take.
    """
    #: Said once the terminal is back in its own mode. A line printed while the
    #: outer tty is raw has no carriage return of its own and climbs the screen
    #: in a staircase, over whatever the provider drew (SYRD-221).
    closing_message = ""
    session = None
    window = None
    #: What the provider has switched on in the person's terminal through us,
    #: so it can be switched off again however the provider ends.
    modes = _TerminalModeLedger()
    #: What the screen reading sees: the same output, re-cut on sequence
    #: boundaries. What is SHOWN is never held back -- the terminal reassembles
    #: a split sequence itself -- only what is judged.
    judged = _TerminalStream()

    def show(chunk: str) -> None:
        modes.feed(chunk)
        if output_write is not None:
            output_write(chunk)
        else:
            sys.stdout.write(chunk)
            sys.stdout.flush()

    # The terminal is taken BEFORE the provider is started, not after. A CLI
    # asks its questions in the first milliseconds it is alive, and a reply
    # that lands while the outer tty is still echoing is drawn on the screen --
    # which is the corruption, arriving in the one window where raw mode was
    # not yet on. Found by the regression test for it, not by reasoning
    # (SYRD-221).
    #
    # And it is held until the provider is GONE, for the same reason at the
    # other end. It used to be handed back first and the provider ended after,
    # still alive, still with focus reporting on -- so every reply and focus
    # event in between reached a cooked, echoing terminal as `^[[...`, which is
    # what test8 showed (SYRD-221 UAT).
    terminal = _RawTerminal(input_fd)
    try:
        with terminal:
            try:
                deadline = monotonic() + timeout_seconds
                # Named BEFORE the provider is started, not after it has been
                # handed the screen. The title is the only thing that says what
                # this window is for, and a step that never gets a provider at
                # all needs it most: on test15 the terminal was already raw, the
                # spawn produced nothing, and the window still read "Switchyard"
                # with a cursor and no prompt. Nothing on the screen said which
                # step it was, what it was waiting for, or that it was waiting
                # (SYRD-245).
                window = _SetupWindowNarrator(
                    cli=cli, purpose=purpose, deadline=deadline, monotonic=monotonic,
                    write=output_write,
                )
                window.opened()
                try:
                    session = (session_factory or PtyForegroundSession)(list(args), **kwargs)
                except OSError as exc:
                    # The provider could not be started. Said here, bounded, and
                    # returned as "not recorded" rather than raised: the phase
                    # below reports which roles are still missing what, and a
                    # traceback out of a raw terminal tells an operator nothing
                    # about which of a dozen worktrees failed (SYRD-245).
                    print_func(window.could_not_start(args=args, cwd=kwargs.get("cwd"), exc=exc))
                    return is_complete()
                recent = ""
                last_output = monotonic()
                last_drawn = last_output
                #: Set when the provider erased its frame and has drawn nothing
                #: since. Kept apart from `recent`, which is empty both then and
                #: before anything was ever drawn.
                awaiting_redraw = False
                #: Only a provider OBSERVED to be finished is sent its exit input.
                #: Typed into anything else it is an answer: into Claude's
                #: "Paste code here" box, `/exit` is pasted as the sign-in code.
                at_prompt = False
                while True:
                    if session.poll() is not None:
                        return is_complete()
                    chunk = session.read()
                    if chunk:
                        show(chunk)
                        chunk = judged.feed(chunk)
                        now = monotonic()
                        replaced_at = _replaced_frame_starts_at(chunk)
                        # A new screen replaces the old one rather than piling on
                        # it -- but only output that DRAWS something is a screen.
                        # Claude reasserts its keyboard modes whenever the window
                        # loses focus, and that arrives exactly when the person
                        # goes to the browser to sign in. Counting it as a screen
                        # forgot the sign-in box and closed it six seconds later.
                        #
                        # Except when the provider says outright that the old
                        # frame is gone: then it is gone NOW, not after a second
                        # of quiet.
                        if replaced_at is not None:
                            recent = chunk[replaced_at:]
                            last_drawn = now
                            awaiting_redraw = not _draws_something(recent)
                        elif _draws_something(chunk):
                            awaiting_redraw = False
                            if now - last_drawn >= PROVIDER_SCREEN_RESET_SECONDS:
                                recent = chunk
                            else:
                                recent = (recent + chunk)[-4096:]
                            last_drawn = now
                        last_output = now
                    if input_fd is not None:
                        session.relay_from(input_fd)
                    # Recorded is not the same as finished. Claude writes its
                    # first run the moment it draws "Do you trust the files in
                    # this folder?" -- measured -- and ending the step there
                    # typed `/exit` into that question, whose Enter confirmed
                    # "No, exit", and left it on screen above the next step.
                    # The person then answered it into `codex login` (SYRD-221
                    # UAT, test9). A screen still asking waits for its answer;
                    # the deadline still bounds it.
                    if is_complete() and _screen_is_settled(
                        recent, awaiting_redraw=awaiting_redraw
                    ):
                        sleep(FOREGROUND_COMPLETION_POLL_SECONDS)
                        closing_message = window.done_at_prompt()
                        at_prompt = True
                        break
                    quiet_for = monotonic() - last_output
                    if (
                        recent
                        and quiet_for >= quiet_seconds
                        and _screen_is_settled(recent, awaiting_redraw=awaiting_redraw)
                    ):
                        closing_message = window.done_at_prompt()
                        at_prompt = True
                        break
                    if monotonic() >= deadline:
                        # Specific about what did not happen, because "gave up
                        # waiting" on its own reads as a Switchyard fault when the
                        # usual cause is a sign-in nobody finished. The resumable
                        # command follows in the report below, with the account
                        # named. Nothing is typed into it: whatever it is still
                        # asking, the exit input would be taken as the answer.
                        closing_message = window.stalled(
                            watching=watching, timeout_seconds=timeout_seconds
                        )
                        break
                    # The window says what it is waiting for, continuously, because a
                    # silent one is indistinguishable from a stopped one -- which is
                    # exactly how this step was reported: "stopped in a standalone
                    # Claude window". It had not stopped; it was waiting for a sign-in
                    # nobody had been told was still outstanding (SYRD-221).
                    window.tick()
                    # A window dragged mid-setup must not leave the provider
                    # drawing to the one it started in.
                    follow = getattr(session, "sync_window_size", None)
                    if follow is not None:
                        follow()
                    # Wait on the descriptors when the session can, so a keystroke
                    # is passed on the moment it is typed rather than at the next
                    # tick. A session that cannot be waited on -- the shape suites
                    # drive -- keeps the plain interval.
                    wait = getattr(session, "wait_for_activity", None)
                    if wait is None:
                        sleep(FOREGROUND_COMPLETION_POLL_SECONDS)
                    else:
                        wait(FOREGROUND_COMPLETION_POLL_SECONDS, input_fd)
                exit_input = PROVIDER_SESSION_EXIT_INPUT.get(cli, "") if at_prompt else ""
                if exit_input and session.poll() is None:
                    # Its own way out first, so it writes whatever it keeps for the
                    # account before it goes -- and switches its own modes off,
                    # which is why what it prints on the way out is still shown.
                    session.write(exit_input)
                    for _ in range(20):
                        if session.poll() is not None:
                            break
                        chunk = session.read()
                        if chunk:
                            show(chunk)
                        sleep(FOREGROUND_COMPLETION_POLL_SECONDS)
            finally:
                if session is not None and session.poll() is None:
                    session.terminate()
                    try:
                        session.wait(timeout=10)
                    except Exception:
                        pass
                # Whatever it left switched on, switched off, through the same
                # channel it was switched on through. A provider that exited
                # cleanly has left nothing and this writes nothing.
                handback = modes.handback()
                if handback:
                    if output_write is not None:
                        output_write(handback)
                    else:
                        sys.stdout.write(handback)
                        sys.stdout.flush()
                    if terminal.engaged:
                        # A focus event the terminal sent before it read the
                        # handback is still in flight; let it land so the flush
                        # on restore discards it rather than the person's shell
                        # echoing it.
                        sleep(0.05)
                if terminal.engaged:
                    # The provider is gone, but these CLIs draw inline, so its
                    # last frame -- a question, or an input box -- stays on the
                    # screen looking live while the next step starts under it.
                    # test9's User answered a finished Claude's trust question
                    # into `codex login` that way. Cleared, not scrolled away:
                    # the scrollback keeps it for anybody who wants to look.
                    if output_write is not None:
                        output_write(SETUP_STEP_CLEAR_SCREEN)
                    else:
                        sys.stdout.write(SETUP_STEP_CLEAR_SCREEN)
                        sys.stdout.flush()
        # Reconciled here, with the provider gone: a CLI can write what it
        # recorded on the way out, and the message has to agree with the result
        # this returns rather than with what the screen looked like a moment
        # before (SYRD-221).
        finished = is_complete()
        if at_prompt and not finished:
            closing_message = window.at_prompt_but_unrecorded()
        if closing_message:
            print_func(closing_message)
        return finished
    finally:
        if session is not None:
            if session.poll() is None:
                session.terminate()
                try:
                    session.wait(timeout=10)
                except Exception:
                    pass
            close = getattr(session, "close", None)
            if close is not None:
                close()
        if window is not None:
            window.closed()
