"""One Ctrl-C ends a setup run, and says what it left (SYRD-785).

The User, aborting `switchyard new` for test13: "I had to press ctrl-c a LOT". A provider's first run holds the
terminal raw, so Ctrl-C arrives as a byte, not a signal, and was passed to the provider. Claude Code takes it as
"press again to exit", so one press ended at most that one step, and `new` went straight on to the next: one sign-in
and then a folder-trust step per role, a dozen presses for a six-role project.

- `interrupt_in` finds Ctrl-C in what the person typed. The provider session (`provider_session`) passes on what came
  before it and raises `SetupInterrupted`, a `KeyboardInterrupt`, which no step catches. The provider's process group
  is stopped by `stop_process_group` -- SIGINT, then SIGTERM, then SIGKILL, each a short bound apart, as
  `switchyard-bounded` does -- and no later step starts. Outside a raw terminal, Ctrl-C is SIGINT and is already a
  KeyboardInterrupt.
- `interrupted` is what `scripts/switchyard` says when Ctrl-C ends a command, with 130 and never a traceback. For
  `switchyard new` -- interrupted in its questions, provisioning, or the sign-in and trust steps -- it says how far
  the run got and how to remove what it left, reading the slug and stages from the command's own frame on the
  interrupt's traceback (`new_progress`).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from typing import Any, Callable, Sequence

#: What a terminal in raw mode sends for Ctrl-C.
CTRL_C = b"\x03"
#: SIGINT first, as the person meant it; then SIGTERM; then SIGKILL. Seconds each is given to end the group.
STOP_SIGNALS: tuple[tuple[signal.Signals, float], ...] = (
    (signal.SIGINT, 1.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0))


class SetupInterrupted(KeyboardInterrupt):
    """The person pressed Ctrl-C while a provider's setup held the terminal."""


#: A complete CSI sequence, as a terminal sends one for a key: ESC [, parameters, intermediates, a final byte.
_CSI = re.compile(rb"\x1b\[([0-?]*)([ -/]*)([@-~])")
#: What may still become one: ESC [ and the parameters so far, at the very end of what was read.
_CSI_PREFIX = re.compile(rb"\x1b\[[0-?]*[ -/]*\Z")
#: The lock bits a terminal may add to any key's modifiers: Caps Lock 64, Num Lock 128.
_LOCK_BITS = 64 | 128
#: Ctrl, and nothing else, as a modifier value encodes it (1 + the modifier bits; Ctrl is 4).
_CTRL_ALONE = 4


def _ctrl_alone(modifier: bytes) -> bool:
    try:
        bits = int(modifier) - 1
    except ValueError:
        return False
    return bits >= 0 and bits & ~_LOCK_BITS == _CTRL_ALONE


def is_ctrl_c(sequence: re.Match[bytes]) -> bool:
    """Whether a complete CSI sequence is Ctrl-C (Ctrl alone, with c) as the two keyboard protocols Claude switches on
    in the person's terminal encode it (SYRD-785 Audit):
    * xterm's modifyOtherKeys: ESC [ 27 ; <modifiers> ; 99 ~
    * the kitty keyboard protocol: ESC [ 99 ; <modifiers> u, or ESC [ 99 ; <modifiers> : 1 u for a press (not a
      repeat, 2, or a release, 3).
    Ctrl+Shift+C -- Konsole's copy -- is not."""
    params, intermediates, final = sequence.groups()
    if intermediates:
        return False
    fields = params.split(b";")
    if final == b"~":
        return len(fields) == 3 and fields[0] == b"27" and fields[2] == b"99" and _ctrl_alone(fields[1])
    if final == b"u" and len(fields) == 2 and fields[0] == b"99":
        modifier, _sep, event = fields[1].partition(b":")
        return _ctrl_alone(modifier) and event in (b"", b"1")
    return False


class InterruptScanner:
    """Finds Ctrl-C in what the person types, in any of its three encodings -- the byte 0x03, xterm modifyOtherKeys,
    kitty -- wherever it falls in a read, and says what to pass on before it. A sequence that a read cut short is held
    until the next read completes it, so it is never relayed half-way before being recognised; `release` gives back
    what is held once nothing more has come, so a lone Escape key still reaches the provider."""

    #: Longer than any key a terminal sends: what is held past this is not a key, and is passed on.
    MAX_HELD = 64

    def __init__(self) -> None:
        self._held = b""

    def feed(self, data: bytes) -> tuple[bytes, bool]:
        """(what to pass on, whether Ctrl-C was pressed): everything typed before the first Ctrl-C, and nothing after
        it."""
        data, self._held = self._held + data, b""
        out = bytearray()
        position = 0
        while position < len(data):
            byte = data[position]
            if byte == CTRL_C[0]:
                return bytes(out), True
            if byte == 0x1B:
                sequence = _CSI.match(data, position)
                if sequence is not None:
                    if is_ctrl_c(sequence):
                        return bytes(out), True
                    out += sequence.group(0)
                    position = sequence.end()
                    continue
                tail = data[position:]
                if (tail == b"\x1b" or _CSI_PREFIX.match(tail)) and len(tail) <= self.MAX_HELD:
                    self._held = tail
                    return bytes(out), False
            out.append(byte)
            position += 1
        return bytes(out), False

    def release(self) -> bytes:
        """What is held, handed back to be passed on: nothing came to complete it."""
        held, self._held = self._held, b""
        return held


def interrupt_in(data: bytes) -> tuple[bytes, bool]:
    """(what to pass on, whether the person pressed Ctrl-C) for one complete read, in any of its encodings."""
    scanner = InterruptScanner()
    before, found = scanner.feed(data)
    return before + (b"" if found else scanner.release()), found


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_group(
    process: subprocess.Popen,
    *,
    signals: tuple[tuple[signal.Signals, float], ...] = STOP_SIGNALS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """End `process` and every process in its group (it leads one: providers are started as session leaders), each
    signal given its bound before the next. Reaps `process`."""
    pgid = process.pid
    for number, bound in signals:
        try:
            os.killpg(pgid, number)
        except ProcessLookupError:
            break
        except PermissionError:
            # A group whose members another account owns, which this process may not signal: the leader at least.
            try:
                process.send_signal(number)
            except ProcessLookupError:
                break
        deadline = monotonic() + bound
        while monotonic() < deadline:
            process.poll()
            if not _group_alive(pgid):
                break
            sleep(0.05)
        if not _group_alive(pgid):
            break
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


# -- switchyard new's abort report ------------------------------------------------------------------------------------
#: The first stage of `switchyard new` that creates anything of the project (`NEW_PROJECT_STAGES`).
FIRST_CREATING_STAGE = "project accounts and files"


def new_progress(traceback: Any) -> dict[str, Any]:
    """How far an interrupted `switchyard new` got: its slug and stages, read from the command's own frame on the
    interrupt's traceback -- the command keeps them as locals once its questions are answered, and nothing else
    of it changes. Empty when the interrupt came during the questions."""
    while traceback is not None:
        code = traceback.tb_frame.f_code
        if code.co_name == "switchyard_new_command" and code.co_filename.endswith("switchyard_new_command.py"):
            local = traceback.tb_frame.f_locals
            return {"slug": local.get("resolved_slug") or "", "stages": local.get("stages")}
        traceback = traceback.tb_next
    return {}


def aborted_message(progress: dict[str, Any] | None) -> str:
    """One message: how far `switchyard new` got, and how to remove what it left."""
    progress = progress or {}
    slug = progress.get("slug") or ""
    stages = progress.get("stages")
    current = getattr(stages, "current", None)
    stage = current[0] if current else ""
    names = tuple(getattr(stages, "names", ()) or ())
    if not slug:
        return "switchyard: new aborted (Ctrl-C) during its questions; nothing was created."
    teardown = (f"`sudo switchyard teardown {slug} --dry-run` lists what is there, and `sudo switchyard teardown "
                f"{slug} --confirm {slug} --destroy-registered-tenant --remove-owner-home --remove-owner-user` "
                "removes it")
    created = (stage in names and FIRST_CREATING_STAGE in names
               and names.index(stage) >= names.index(FIRST_CREATING_STAGE))
    if not created:
        where = f"during {stage}" if stage else "before it created anything"
        return (f"switchyard: new {slug} aborted (Ctrl-C) {where}; none of {slug}'s accounts, files or board were "
                f"created. If you are unsure, {teardown}.")
    return (f"switchyard: new {slug} aborted (Ctrl-C) during {stage}; {slug} is partly created and was not started. "
            f"{teardown[0].upper() + teardown[1:]}.")


def interrupted(arguments: Sequence[str], interrupt: BaseException) -> tuple[str, int]:
    """What `switchyard` says when Ctrl-C ends a command, and its exit status: for `new`, how far it got and how to
    remove what it left; for any other command, one line. Never a traceback."""
    command = arguments[0] if arguments else "command"
    if command == "new":
        return aborted_message(new_progress(interrupt.__traceback__)), 130
    return f"switchyard: {command} aborted (Ctrl-C); nothing after the step it was in ran.", 130
