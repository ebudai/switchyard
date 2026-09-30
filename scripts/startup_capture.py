"""What a role's command printed when it exited during startup (SYRD-532).

A role's pane runs its CLI directly under tmux. When the CLI exits, tmux
closes the pane and the session with it, so a start that failed left only
"did not leave a live session": MEFP's luna-6 exited after its runtime switch,
and neither its exit status nor anything it printed survived anywhere.

The start now arms its new session, in the same tmux invocation that creates
it, so no pane can die first: `remain-on-exit` holds the dead pane for one
moment and a `pane-died` hook saves its last lines -- including tmux's own
"Pane is dead (status N, ...)" line -- into a file only this account can read,
then kills the session. The session therefore still disappears when its
command exits, as it always did, and every check that asks whether it exists
answers exactly as before. A start that is verified, or left running
unverified, is disarmed again, so a session that stays up behaves exactly as
an unarmed one would afterwards.

Measured on tmux 3.7c and 3.2a (Zorin's): a format in the hook's `-t` is not
expanded there, so the hook uses its implicit target, the dead pane and its
session.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Lines of the dead pane kept, and the most of the file ever read back.
CAPTURE_LINES = 40
CAPTURE_READ_LIMIT = 16384
_DEAD_STATUS = re.compile(r"Pane is dead \(status (-?\d+)")


def capture_path(session_dir: Path, session_file_stem: str) -> Path:
    return session_dir.expanduser() / f"{session_file_stem}.startup-exit"


def prepare(path: Path) -> Path | None:
    """Create or empty the capture file, readable and writable by this account only.

    None when it cannot be made, or when its path cannot be quoted for tmux;
    the start then proceeds unarmed, exactly as before.
    """
    if "'" in str(path) or "\n" in str(path):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    return path


def arm_args(session: str, path: Path | None) -> list[str]:
    """tmux commands chained after `new-session`, in the same invocation."""
    if path is None:
        return []
    buffer = f"switchyard-startup-{session}"
    hook = (
        f"capture-pane -J -S -{CAPTURE_LINES} -b '{buffer}' ; "
        f"save-buffer -b '{buffer}' '{path}' ; "
        f"delete-buffer -b '{buffer}' ; "
        "kill-session"
    )
    return [
        ";", "set-window-option", "-t", session, "remain-on-exit", "on",
        ";", "set-hook", "-t", session, "pane-died", hook,
    ]


def disarm_args(session: str) -> list[str]:
    return ["tmux", "set-hook", "-u", "-t", session, "pane-died",
            ";", "set-window-option", "-u", "-t", session, "remain-on-exit"]


def report(role_name: str, path: Path | None) -> str:
    """One line for a start that did not leave a live session: the exit status, and where its output is.

    The output itself stays in the file: it can hold whatever the CLI put on
    screen, and it belongs to this account.
    """
    if path is None:
        return ""
    try:
        with path.open("rb") as handle:
            text = handle.read(CAPTURE_READ_LIMIT).decode("utf-8", "replace")
    except OSError:
        return ""
    if not text.strip():
        return ""
    match = _DEAD_STATUS.search(text)
    status = f"exited with status {match.group(1)}" if match else "exited"
    return (
        f"team-launcher: role {role_name}'s command {status} during startup; "
        f"its last lines are in {path} (readable by this account only)"
    )
