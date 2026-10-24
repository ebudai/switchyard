"""What an execution lease is, shared by the bounded wrapper and the listener (SYRD-616).

A role can start a command that never ends. While it runs under the role's
current turn, nothing that watches later deliveries (SYRD-557, SYRD-583,
SYRD-636) ever fires: the work looks like the role's own. A lease gives each
long command under a role's pane a deadline the board can see:

* **Identity.** One session of its own under the pane -- the unit a provider's
  tool starts a command in -- named by its leader's pid and start tick, so a
  reused pid is a different lease (`background_child.Holder.key`).
* **Deadline.** A wall clock, never activity: a valid test can be silent and an
  infinite loop can print forever. It is the default observation threshold
  from the session's start, unless the command was started through
  `switchyard-bounded`, which declares a longer expected budget (`--expect`)
  or a hard deadline it enforces itself (`--deadline`), or the Director has
  extended it.
* **Declaration.** `switchyard-bounded` writes one small file per session
  beside the pane-state files, `leases/<session>-<leader tick>.json`; the
  listener reads it when it observes that session. A declaration is a claim by
  the role, so its budget is capped.
* **Interrupt marker.** Before the listener carries out the Director's
  interrupt it writes `<session>-<leader tick>.interrupt`, so a wrapped command
  can say who stopped it and why.

Nothing from a command's environment or output is ever read, and its argv is
recorded only as `sanitize_command` leaves it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

#: The default observation threshold for a command nobody bounded (ticket requirement 5).
DEFAULT_OBSERVE_SECONDS = 15 * 60.0
OBSERVE_ENV = "TICKET_BOARD_LEASE_OBSERVE_SECONDS"
#: The longest budget a role may declare for itself before launch.
DEFAULT_MAX_DECLARED_SECONDS = 6 * 3600.0
MAX_DECLARED_ENV = "TICKET_BOARD_LEASE_MAX_DECLARED_SECONDS"
#: How much of a command line is kept.
COMMAND_CHARS = 240

_DURATION = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)([smhd]?)$")
_UNIT = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
MASK = "[redacted]"


def parse_duration(text: str) -> float:
    """Seconds in `90`, `90s`, `20m`, `1.5h` or `1d`; ValueError otherwise."""
    found = _DURATION.match(str(text).strip())
    if not found:
        raise ValueError(f"not a duration: {text!r} (use e.g. 90s, 20m, 1.5h)")
    return float(found.group(1)) * _UNIT[found.group(2)]


def configured_seconds(env_name: str, default: float, environ: dict[str, str] | None = None) -> float:
    raw = (os.environ if environ is None else environ).get(env_name, "").strip()
    try:
        value = parse_duration(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


_PROGRAM = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_LONG_OPTION = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHORT_OPTION = re.compile(r"^-[A-Za-z0-9]$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _existing_path(word: str, cwd: str, exists) -> bool:
    if not word or len(word) > 4096 or "\0" in word:
        return False
    try:
        return bool(exists(word if word.startswith("/") or not cwd else os.path.join(cwd, word)))
    except (OSError, ValueError):
        return False


def sanitize_command(argv: list[str] | tuple[str, ...], *, cwd: str = "", exists=os.path.exists) -> str:
    """What may be recorded of a command line: what is provably safe, and `[redacted]` for everything else.

    Kept: the program's name; option names (`-H`, `--deadline`, and `--name=`
    with its value always masked); the name of an environment assignment
    (`NAME=[redacted]`); and a standalone argument that names a path existing
    on disk, relative to the process's directory. Masked: every other argument -- a
    value, a header, a JSON document, a whole `-c` script -- however it is
    spelled, because no rule that looks for secrets can be complete (Audit's
    kick-backs of da0689a, 80d9d7e and 5caba63). A summary for a person, never
    a command to run again (SYRD-616).
    """
    parts = [str(part) for part in argv]
    if not parts:
        return ""
    program = os.path.basename(parts[0])
    words = [program if _PROGRAM.match(program) else MASK]
    for part in parts[1:]:
        name, equals, value = part.partition("=")
        if part in ("-", "--") or _SHORT_OPTION.match(part) or _LONG_OPTION.match(part):
            words.append(part)
        elif equals and _LONG_OPTION.match(name):
            words.append(f"{name}={MASK}")  # always: a path is no proof when an option names what it holds
        elif part.startswith("-") and _SHORT_OPTION.match(part[:2]):
            words.append(part[:2] + MASK)  # `-pS3cret`: the flag, never what is attached to it
        elif equals and _ENV_NAME.match(name):
            words.append(f"{name}={MASK}")
        elif _existing_path(part, cwd, exists):
            words.append(part)
        else:
            words.append(MASK)
    # ASCII only, and bounded with an ASCII mark: a board in SQL_ASCII counts bytes, and cutting a multibyte
    # character there leaves a row no UTF-8 reader can read.
    line = " ".join(words).encode("ascii", errors="replace").decode("ascii")
    return line if len(line) <= COMMAND_CHARS else line[:COMMAND_CHARS - 3] + "..."


def lease_dir(pane_state_dir: Path | None = None) -> Path:
    """Where declarations and interrupt markers live: beside the pane-state files the listener already reads."""
    if pane_state_dir is None:
        from .pane_state import DEFAULT_PANE_STATE_DIR as pane_state_dir  # type: ignore[no-redef]
    return Path(pane_state_dir) / "leases"


def start_ticks(pid: int, proc_root: Path = Path("/proc")) -> int:
    """When `pid` started, in clock ticks since boot; 0 when unreadable."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        return int(stat[stat.rfind(")") + 2:].split()[19])
    except (OSError, IndexError, ValueError):
        return 0


def session_name(session: int, leader_ticks: int) -> str:
    return f"{session}-{leader_ticks}"


def _write_json(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".lease.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > 16384:
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def declare(directory: Path, session: int, leader_ticks: int, *, deadline: float, hard: bool, command: str,
            label: str = "", run: str = "") -> Path:
    """Write the declaration for a session: its deadline, whether it is enforced, and what runs."""
    path = directory / (session_name(session, leader_ticks) + ".json")
    _write_json(path, {"session": session, "leader_ticks": leader_ticks, "deadline": float(deadline),
                       "source": "hard" if hard else "declared", "hard": bool(hard), "command": command,
                       "label": label, "run": run, "declared_at": time.time(), "wrapper_pid": os.getpid()})
    return path


def declaration(directory: Path, session: int, leader_ticks: int, *, now: float, environ: dict[str, str] | None = None
                ) -> dict[str, Any] | None:
    """A session's declaration, if any, with its budget capped; None when there is none or it is unreadable."""
    record = _read_json(directory / (session_name(session, leader_ticks) + ".json"))
    if record is None:
        return None
    try:
        deadline = float(record["deadline"])
        declared_at = float(record.get("declared_at") or now)
    except (KeyError, TypeError, ValueError):
        return None
    cap = configured_seconds(MAX_DECLARED_ENV, DEFAULT_MAX_DECLARED_SECONDS, environ)
    hard = record.get("hard") is True
    return {"deadline": min(deadline, declared_at + cap), "source": "hard" if hard else "declared", "hard": hard,
            "label": str(record.get("label") or "")[:120], "run": str(record.get("run") or "")[:120]}


def withdraw(directory: Path, session: int, leader_ticks: int) -> None:
    try:
        (directory / (session_name(session, leader_ticks) + ".json")).unlink()
    except OSError:
        pass


def mark_interrupt(directory: Path, session: int, leader_ticks: int, *, actor: str, reason: str) -> None:
    _write_json(directory / (session_name(session, leader_ticks) + ".interrupt"),
                {"actor": actor, "reason": reason[:400], "at": time.time()})


def interrupt_marker(directory: Path, session: int, leader_ticks: int) -> dict[str, Any] | None:
    return _read_json(directory / (session_name(session, leader_ticks) + ".interrupt"))
