"""Why a role's pane resumed the conversation it did, or started fresh, kept beside its session record (SYRD-569).

A start decides between resuming the recorded conversation and starting fresh,
and said why only on the stderr of whatever process started it: the terminal
`switchyard <project>` ran in, a pane that tmux then drew over, or nothing at
all. MEFP's Main came up fresh on 2026-10-13 and the only log left, Konsole's,
was empty, so nobody could say whether there was no record, the provider had
lost the conversation, or the resume died.

Each decision is now also appended, one JSON object per line, to
`<session_dir>/<pane>.resume.log`, the directory the record itself is in:

- the launcher writes what a start decided and why (`by: launcher`);
- the pane hook writes each time it records another conversation for the pane,
  and each time it keeps the record against a conversation it does not accept
  (`by: hook`), so the history also says how the record came to hold what the
  next start read.

The hook is a standalone program and keeps its own copy of the append; both
write this format at this path. The file is private to the account and kept
under `RESUME_HISTORY_MAX_BYTES`: past that, the older half is dropped. A
failure to write it never fails a start.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig

RESUME_HISTORY_SUFFIX = ".resume.log"
RESUME_HISTORY_MAX_BYTES = 256 * 1024


def resume_history_path(target: str, session_dir: Path) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
    return session_dir / f"{safe}{RESUME_HISTORY_SUFFIX}"


def append_resume_history(path: Path, entry: dict[str, Any]) -> None:
    """Append one entry; drop the older half first when the file has grown past the limit."""
    line = (json.dumps(entry, sort_keys=True, default=str) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size + len(line) > RESUME_HISTORY_MAX_BYTES:
            kept = path.read_bytes().splitlines(keepends=True)
            kept = kept[len(kept) // 2 :]
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"".join(kept))
            os.replace(tmp, path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(line)
    except OSError as exc:
        print(f"team-launcher: could not keep resume history in {path}: {exc}", file=sys.stderr)


def records_seen(target: str, session_dir: Path) -> dict[str, Any]:
    """The pane's record files a start could read, with what each names: its own, set aside, and the shared store's."""
    safe = resume_history_path(target, session_dir).name.removesuffix(RESUME_HISTORY_SUFFIX)
    places = [session_dir]
    if session_dir.parent.name == "roles":  # role_session_dir: the project's shared store is two levels up
        places.append(session_dir.parent.parent)
    seen: dict[str, Any] = {}
    for place in places:
        for suffix in ("", ".superseded", ".superseded.1", ".resume_timeout"):
            path = place / f"{safe}.json{suffix}"
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                mtime = path.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                payload = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else {}
                seen[str(path)] = {key: value for key, value in (
                    ("session_id", parsed.get("session_id")), ("target", parsed.get("target")),
                    ("source", parsed.get("source")), ("updated_at", parsed.get("updated_at")), ("mtime", mtime),
                    ("transcript_path", payload.get("transcript_path"))) if value not in (None, "")}
    return seen


def record_resume_decision(
    role: RoleConfig,
    session_dir: Path,
    event: str,
    *,
    entry: str = "",
    session_id: str = "",
    detail: str = "",
) -> None:
    """One launcher decision about `role`'s conversation: what the start did, and the message it printed."""
    append_resume_history(
        resume_history_path(role.target, session_dir),
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "time": time.time(),
            "by": "launcher",
            "pid": os.getpid(),
            "role": role.role,
            "target": role.target,
            "entry": entry,
            "event": event,
            "session_id": session_id,
            "detail": detail,
            "records": records_seen(role.target, session_dir),
        },
    )


def read_resume_history(target: str, session_dir: Path) -> list[dict[str, Any]]:
    """Every entry still kept for the pane, oldest first; a line that is not JSON is skipped."""
    try:
        lines = resume_history_path(target, session_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries
