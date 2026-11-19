#!/usr/bin/env python3
"""Ctrl-V in a presentation tab hands the clipboard's image to the role on screen there (SYRD-834).

Since SYRD-791 no role pane can read the desktop owner's clipboard: the tenant's compositor socket withholds the
data-control protocol, which is what stops an agent reading the clipboard at will. `switchyard paste-image` is how
the owner hands one image over, and this is the same handover, on the key the owner already presses.

`switchyard-pane-window` runs this between Konsole and the tab's attach, as the desktop owner, in the owner's own
session. It is a pty relay:

* Every byte typed or pasted passes through unchanged, and so does every byte the attach writes, at once. Window
  size changes are passed on. Konsole's own Ctrl+Shift+V paste arrives as bracketed text, never as Ctrl-V, and is
  passed through like everything else.
* Ctrl-V (0x16) typed on its own is held. When the clipboard offers an image, it is handed to the project through
  `paste_image.pane_image` -- the policy check, the reader and the grant directory of `paste-image` -- and the
  image's path is typed into this tab's pane as a bracketed paste, which the role's CLI takes as an attachment.
  When the clipboard holds text or nothing, the Ctrl-V is passed on, so the CLI sees what it always saw. When the
  handover fails, the split's header says why for a few seconds and nothing reaches the pane.
* Only this tab's keys reach this relay, so "only when Switchyard has focus" needs no check: every other window's
  Ctrl-V never comes here. A 0x16 inside a paste, or inside an escape sequence the terminal sends, is data.

Nothing here reads the clipboard but the handover, which runs as a child process so that a reader that hangs can be
ended. A project with no approved desktop policy -- a headless one, or one that runs as this account -- gets no relay
at all: the attach is executed in its place. When the attach ends, so does this, with its status, and the pane
window says the tab is inert (SYRD-43).
"""

from __future__ import annotations

import collections
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

CTRL_V = 0x16
#: What a bracketed paste starts and ends with, from the terminal and to the pane alike.
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
#: How long the clipboard is given, the image's read included, before the handover is ended.
HANDOVER_SECONDS = 20.0
#: How long a refusal stays in the split's header.
MESSAGE_SECONDS = 8.0
MESSAGE_MAX = 120
#: How long an unfinished escape sequence from the terminal waits for its next byte. A terminal sends a sequence in
#: one write; a key sends a lone ESC (the Escape key) or a prefix (Alt+], Alt+P) and nothing more. Without this, the
#: next key -- a Ctrl-V seconds later -- was read as that sequence's end, and the image never handed over.
ESCAPE_SECONDS = 0.05
#: The longest OSC string kept, to read the header the attach sets.
OSC_MAX = 1024

_GROUND, _ESC, _ESC_INTERMEDIATE, _CSI, _STRING, _PASTE = range(6)
_GROUND_STOP = re.compile(rb"[\x1b\x16]")
_STRING_STOP = re.compile(rb"[\x07\x1b]")


class InputScanner:
    """Finds the Ctrl-Vs a person typed in what the terminal sends: outside any paste and any escape sequence."""

    def __init__(self) -> None:
        self.state = _GROUND
        self.params = bytearray()
        self.tail = b""

    @property
    def mid_sequence(self) -> bool:
        """True while an escape sequence the terminal began is unfinished (a paste is not one: it has its end)."""
        return self.state not in (_GROUND, _PASTE)

    def settle(self) -> None:
        """No next byte came in time: what began a sequence was a key, and the next byte stands on its own."""
        if self.mid_sequence:
            self.state = _GROUND

    def feed(self, data: bytes) -> list[bytes | None]:
        """`data` split around each typed Ctrl-V, which is None; every other byte kept, in order."""
        pieces: list[bytes | None] = []
        start = index = 0
        size = len(data)
        while index < size:
            if self.state == _PASTE:
                window = self.tail + data[index:]
                found = window.find(PASTE_END)
                if found < 0:
                    self.tail = window[-(len(PASTE_END) - 1):]
                    break
                index += found + len(PASTE_END) - len(self.tail)
                self.tail = b""
                self.state = _GROUND
            elif self.state == _GROUND:
                match = _GROUND_STOP.search(data, index)
                if match is None:
                    break
                index = match.end()
                if data[match.start()] == CTRL_V:
                    pieces.append(data[start:match.start()])
                    pieces.append(None)
                    start = index
                else:
                    self.state = _ESC
            elif self.state == _STRING:
                match = _STRING_STOP.search(data, index)
                if match is None:
                    break
                index = match.end()
                self.state = _GROUND if data[match.start()] == 0x07 else _ESC
            else:
                self._step(data[index])
                index += 1
        pieces.append(data[start:])
        return [piece for piece in pieces if piece != b""]

    def _step(self, byte: int) -> None:
        if self.state == _ESC:
            if byte == 0x5B:  # [
                self.state, self.params = _CSI, bytearray()
            elif byte in b"]P_^X":
                self.state = _STRING
            elif 0x20 <= byte <= 0x2F:
                self.state = _ESC_INTERMEDIATE
            else:
                self.state = _GROUND
        elif self.state == _ESC_INTERMEDIATE:
            if not 0x20 <= byte <= 0x2F:
                self.state = _GROUND
        elif self.state == _CSI:
            if 0x30 <= byte <= 0x3F:
                self.params.append(byte)
            elif 0x40 <= byte <= 0x7E:
                pasting = byte == 0x7E and self.params == b"200"
                self.state = _PASTE if pasting else _GROUND
                self.tail = b""
            elif byte == 0x1B:
                self.state = _ESC


class OutputWatcher:
    """What the attach has asked of the terminal, read from what it writes: bracketed paste, the split's header, and
    whether its output stopped between two sequences, where something else may be written."""

    def __init__(self, header: str = "") -> None:
        self.state = _GROUND
        self.params = bytearray()
        self.osc: bytearray | None = None
        self.bracketed = False
        self.header = header
        self._recent = b""

    @property
    def between_sequences(self) -> bool:
        """True when a write now would not land inside an escape sequence or a UTF-8 character."""
        if self.state != _GROUND:
            return False
        recent = self._recent
        for back in range(1, min(len(recent), 3) + 1):
            byte = recent[-back]
            if byte & 0xC0 == 0x80:
                continue
            if byte >= 0xC0:
                return (2 if byte < 0xE0 else 3 if byte < 0xF0 else 4) <= back
            return True
        return True

    def feed(self, data: bytes) -> None:
        self._recent = (self._recent + data)[-4:]
        index, size = 0, len(data)
        while index < size:
            if self.state == _GROUND:
                found = data.find(b"\x1b", index)
                if found < 0:
                    return
                index = found + 1
                self.state = _ESC
            elif self.state == _STRING:
                match = _STRING_STOP.search(data, index)
                if match is None:
                    self._keep(data[index:])
                    return
                self._keep(data[index:match.start()])
                index = match.end()
                self._end_string()
                self.state = _GROUND if data[match.start()] == 0x07 else _ESC
            else:
                self._step(data[index])
                index += 1

    def _keep(self, chunk: bytes) -> None:
        if self.osc is not None and len(self.osc) < OSC_MAX:
            self.osc.extend(chunk[: OSC_MAX - len(self.osc)])

    def _end_string(self) -> None:
        if self.osc is not None and self.osc.startswith(b"30;"):
            self.header = self.osc[3:].decode("utf-8", "replace")
        self.osc = None

    def _step(self, byte: int) -> None:
        if self.state == _ESC:
            if byte == 0x5B:
                self.state, self.params = _CSI, bytearray()
            elif byte in b"]P_^X":
                self.state = _STRING
                self.osc = bytearray() if byte == 0x5D else None
            elif 0x20 <= byte <= 0x2F:
                self.state = _ESC_INTERMEDIATE
            else:
                self.state = _GROUND
        elif self.state == _ESC_INTERMEDIATE:
            if not 0x20 <= byte <= 0x2F:
                self.state = _GROUND
        elif self.state == _CSI:
            if 0x30 <= byte <= 0x3F:
                self.params.append(byte)
            elif 0x40 <= byte <= 0x7E:
                if byte in b"hl" and self.params.startswith(b"?") and b"2004" in self.params[1:].split(b";"):
                    self.bracketed = byte == 0x68
                self.state = _GROUND
            elif byte == 0x1B:
                self.state = _ESC


def brief(message: str) -> str:
    """A refusal short enough for a split's header: its first sentence, as plain text."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", message).strip()
    text = re.sub(r"^switchyard:\s*", "", text)
    first = re.split(r"(?<=[.;])\s", text, maxsplit=1)[0].rstrip(".;")
    first = f"Ctrl-V: {first}"
    return first if len(first) <= MESSAGE_MAX else first[: MESSAGE_MAX - 1] + "…"


def typed_path(path: str, bracketed: bool) -> bytes:
    """What the pane is sent for a handed-over image: its path, as the terminal would paste it."""
    clean = path.encode("utf-8")
    return PASTE_START + clean + PASTE_END if bracketed else clean


class Relay:
    def __init__(self, argv: list[str], project: str, title: str) -> None:
        self.argv = argv
        self.project = project
        self.watcher = OutputWatcher(title)
        self.scanner = InputScanner()
        self.queue: collections.deque[bytes | None] = collections.deque()
        self.to_child = bytearray()
        self.to_terminal_later: list[bytes] = []
        self.handover: subprocess.Popen[bytes] | None = None
        self.handover_output = b""
        self.handover_deadline = 0.0
        self.restore_header_at: float | None = None
        self.settle_input_at: float | None = None
        self.child = 0
        self.master = -1

    # -- the terminal ------------------------------------------------------------------------------------------------
    def _write_terminal(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]

    def _from_attach(self, data: bytes) -> None:
        """The attach's output, unchanged, then whatever was waiting for its output to reach a boundary."""
        self.watcher.feed(data)
        self._write_terminal(data)
        if self.to_terminal_later and self.watcher.between_sequences:
            later, self.to_terminal_later = self.to_terminal_later, []
            for chunk in later:
                self._write_terminal(chunk)

    def _say(self, data: bytes) -> None:
        """Write to the terminal at the first point the attach's output allows."""
        if self.watcher.between_sequences:
            self._write_terminal(data)
        else:
            self.to_terminal_later.append(data)

    def _header(self, text: str) -> bytes:
        return b"\x1b]30;" + re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).encode("utf-8") + b"\x07"

    def _show(self, message: str) -> None:
        self._say(self._header(brief(message)))
        self.restore_header_at = time.monotonic() + MESSAGE_SECONDS

    # -- input -------------------------------------------------------------------------------------------------------
    def _drain_queue(self) -> None:
        while self.queue and self.handover is None:
            item = self.queue.popleft()
            if item is None:
                self._start_handover()
            else:
                self.to_child += item

    def _start_handover(self) -> None:
        argv = [sys.executable, "-I", str(Path(__file__).resolve()), "--hand-over", self.project]
        try:
            self.handover = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._show(f"the handover could not start: {exc}")
            return
        self.handover_output = b""
        self.handover_deadline = time.monotonic() + HANDOVER_SECONDS

    def _finish_handover(self, timed_out: bool = False) -> None:
        process, self.handover = self.handover, None
        assert process is not None
        if timed_out:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        try:
            answer = json.loads(self.handover_output.decode("utf-8"))
        except ValueError:
            answer = None
        if timed_out:
            answer = {"error": f"the clipboard did not answer within {HANDOVER_SECONDS:.0f} seconds"}
        elif not isinstance(answer, dict):
            answer = {"error": "the handover ended without an answer"}
        if answer.get("image"):
            self.to_child += typed_path(str(answer["image"]), self.watcher.bracketed)
        elif answer.get("none"):
            self.to_child.append(CTRL_V)
        else:
            self._show(str(answer.get("error") or "the handover failed"))
        self._drain_queue()

    # -- the loop ----------------------------------------------------------------------------------------------------
    def run(self) -> int:
        try:
            saved = termios.tcgetattr(0)
            size = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
            self.master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
        except OSError:
            # No relay rather than no pane: the attach runs as it did before there was one.
            os.execvp(self.argv[0], self.argv)
        wake_read, wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        signal.set_wakeup_fd(wake_write)
        events: list[int] = []
        for number in (signal.SIGWINCH, signal.SIGCHLD, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(number, lambda signum, _frame: events.append(signum))
        # Raw before the attach starts: it queries the terminal in its first moments, and a cooked line discipline
        # would echo the answers into the screen.
        tty.setraw(0)
        try:
            self.child = os.fork()
            if self.child == 0:  # pragma: no cover - the attach's side
                try:
                    os.setsid()
                    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                    for fd in (0, 1, 2):
                        os.dup2(slave, fd)
                    os.close(slave)
                    os.close(self.master)
                    os.execvp(self.argv[0], self.argv)
                finally:
                    os._exit(127)
            os.close(slave)
            os.set_blocking(self.master, False)
            status = self._loop(events, wake_read)
        finally:
            # Only after the attach is gone, so nothing it or the terminal still sends meets a cooked, echoing tty.
            termios.tcsetattr(0, termios.TCSAFLUSH, saved)
        return status

    def _loop(self, events: list[int], wake_read: int) -> int:
        while True:
            while events:
                signum = events.pop(0)
                if signum == signal.SIGWINCH:
                    with _suppress(OSError):
                        size = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
                        fcntl.ioctl(self.master, termios.TIOCSWINSZ, size)
                elif signum == signal.SIGCHLD:
                    # The attach may end while something it started still holds its terminal open: its end is
                    # this tab's end all the same.
                    pid, status = os.waitpid(self.child, os.WNOHANG)
                    if pid:
                        self._stop_handover()
                        return _exit_code(status)
                else:
                    return self._end_child(signal.SIGHUP)
            now = time.monotonic()
            deadlines = [at for at in (self.restore_header_at, self.settle_input_at,
                                       self.handover_deadline if self.handover else None) if at is not None]
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            readers = [0, self.master, wake_read]
            if self.handover is not None and self.handover.stdout is not None:
                readers.append(self.handover.stdout.fileno())
            writers = [self.master] if self.to_child else []
            try:
                readable, writable, _ = select.select(readers, writers, [], timeout)
            except InterruptedError:
                continue
            if wake_read in readable:
                with _suppress(OSError):
                    os.read(wake_read, 512)
            if self.master in readable:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    data = b""
                if not data:
                    return self._reap()
                self._from_attach(data)
            if 0 in readable:
                try:
                    data = os.read(0, 65536)
                except OSError:
                    data = b""
                if not data:
                    return self._end_child(signal.SIGHUP)
                self.queue.extend(self.scanner.feed(data))
                self._drain_queue()
                self.settle_input_at = time.monotonic() + ESCAPE_SECONDS if self.scanner.mid_sequence else None
            elif self.settle_input_at is not None and time.monotonic() >= self.settle_input_at:
                self.settle_input_at = None
                self.scanner.settle()
            if self.handover is not None and self.handover.stdout is not None \
                    and self.handover.stdout.fileno() in readable:
                chunk = os.read(self.handover.stdout.fileno(), 65536)
                if chunk:
                    self.handover_output += chunk[: 65536 - len(self.handover_output)]
                else:
                    self._finish_handover()
            if self.handover is not None and time.monotonic() >= self.handover_deadline:
                self._finish_handover(timed_out=True)
            if self.restore_header_at is not None and time.monotonic() >= self.restore_header_at:
                self.restore_header_at = None
                self._say(self._header(self.watcher.header))
            if self.to_child and (self.master in writable or not writers):
                try:
                    written = os.write(self.master, self.to_child)
                except BlockingIOError:
                    written = 0
                except OSError:
                    return self._reap()
                del self.to_child[:written]

    def _stop_handover(self) -> None:
        if self.handover is not None:
            self.handover.kill()
            self.handover.wait()
            self.handover = None

    def _reap(self) -> int:
        self._stop_handover()
        _, status = os.waitpid(self.child, 0)
        return _exit_code(status)

    def _end_child(self, signum: int) -> int:
        """The terminal went away, or this was asked to end: the attach is told as a closed terminal tells it."""
        with _suppress(ProcessLookupError):
            os.killpg(self.child, signum)
        with _suppress(OSError):
            os.close(self.master)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.child, os.WNOHANG)
            if pid:
                return _exit_code(status)
            time.sleep(0.05)
        with _suppress(ProcessLookupError):
            os.killpg(self.child, signal.SIGKILL)
        return self._reap()


def _exit_code(status: int) -> int:
    """A wait status as a shell reports it: the exit code, or 128 plus the signal that ended the process."""
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


class _suppress:
    def __init__(self, *kinds: type[BaseException]) -> None:
        self.kinds = kinds

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind, _value, _traceback) -> bool:
        return kind is not None and issubclass(kind, self.kinds)


def hand_over(project: str) -> dict[str, str | bool]:
    """The handover's own process: one answer, as JSON, for the relay that started it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import desktop_access, paste_image

    try:
        image = paste_image.pane_image(project)
    except (paste_image.PasteImageError, desktop_access.DesktopAccessError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # whatever it was, the tab says so and the pane gets nothing
        return {"error": f"the handover failed: {exc.__class__.__name__}: {exc}"}
    return {"image": str(image)} if image is not None else {"none": True}


def relay_offered(project: str) -> bool:
    """Whether this tab's Ctrl-V can hand anything over; asked once, as the tab opens."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts import paste_image

        return paste_image.pane_handoff_offered(project)
    except Exception:
        return False


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--hand-over":
        print(json.dumps(hand_over(argv[1])), flush=True)
        return 0
    project, title = "", ""
    while len(argv) >= 2 and argv[0] in ("--project", "--title"):
        if argv[0] == "--project":
            project = argv[1]
        else:
            title = argv[1]
        argv = argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("switchyard: switchyard_pane_relay needs the pane command to run", file=sys.stderr)
        return 2
    if not (project and os.isatty(0) and relay_offered(project)):
        os.execvp(argv[0], argv)
    return Relay(argv, project, title).run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
