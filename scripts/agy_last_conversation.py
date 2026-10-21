"""A role on agy with no record resumes the conversation agy last opened in its worktree (SYRD-622).

agy opens a conversation the User `/resume`s in the pane without a
SessionStart, so the pane's hooks first hear of it at its next turn. A role
that takes no turn between the `/resume` and the next start -- an idle
designer, over a power cut -- has nothing recorded, and started fresh again:
Otto's designer did, on 2026-10-18, though agy itself had kept the answer the
whole time. agy writes `cache/last_conversations.json` in its store, mapping
each working directory to the conversation it last opened there, at the
moment it opens one (measured on agy 1.4.2: the file's mtime is the resume's
own second; a resume does not touch the conversation's database).

So when a role that resumes with `agy --conversation` has no record, the
conversation agy maps to the role's worktree is taken as its record, and the
normal resume preflight then checks agy still has it. Taken only when:

* the role resumes at all, and not fresh for each ticket;
* agy wrote the map after the launcher last set this role's record aside: a
  record set aside on purpose (a runtime or model change, a resume that
  failed or a conversation the preflight refused) is not brought back by a
  map that predates it, only by a conversation opened since;
* the record it set aside, if any, was agy's own: a role that came to agy
  from another runtime does not inherit an older agy conversation;
* the map names the worktree and a conversation id of agy's form. The file is
  read without following a link, and only up to `MAX_BYTES`.

Each decision, taken or not, is kept in the pane's resume history.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.session_records import _session_record_at_path_for_role

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig

LAST_CONVERSATIONS = Path("cache") / "last_conversations.json"
MAX_BYTES = 1 << 20
#: agy's conversation ids, as its store names them.
CONVERSATION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: The source of a record taken from agy's map, and the prefix of the records agy's hooks write.
SOURCE = "agy.last_conversations"
AGY_HOOK_SOURCE_PREFIX = "gemini."


def _read_map(path: Path) -> tuple[dict[str, Any] | None, float, str]:
    """agy's map and when it was written, or None and why not."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, 0.0, "absent"
    except OSError as exc:
        return None, 0.0, f"unreadable ({exc.strerror or exc})"
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        return None, 0.0, "not a regular file"
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return None, 0.0, f"larger than {MAX_BYTES} bytes"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, 0.0, "not JSON"
    if not isinstance(parsed, dict):
        return None, 0.0, "not a map"
    return parsed, info.st_mtime, ""


def _conversation_for(mapping: dict[str, Any], workdir: str) -> str:
    """The conversation agy maps to the worktree, as the pane names it or as it resolves."""
    for key in dict.fromkeys((workdir, str(Path(workdir).expanduser()), str(Path(workdir).expanduser().resolve()))):
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _skip(role: RoleConfig, session_dir: Path, why: str, session_id: str = "") -> None:
    from scripts.role_sessions import _decided

    _decided(role, session_dir, "", "agy_map_not_taken", f"team-launcher: {why}", session_id)


def adopt_agy_last_conversation(role: RoleConfig, session_dir: Path) -> None:
    """With no record, take the conversation agy last opened in the role's worktree as the role's record."""
    from scripts import team_launcher as launcher
    from scripts.provider_resume import (_uses_agy_conversation_resume, agy_conversation_root_for_session_dir,
                                         agy_conversation_store_exists)

    if not _uses_agy_conversation_resume(role) or launcher._uses_fresh_session_per_ticket(role):
        return
    name = launcher.session_file_name(role.target)
    if launcher.session_id_for_role(role, session_dir):
        return
    path = agy_conversation_root_for_session_dir(session_dir) / LAST_CONVERSATIONS
    mapping, written_at, unread = _read_map(path)
    if mapping is None:
        if unread != "absent":
            _skip(role, session_dir, f"agy's conversation map {path} was not read: {unread}")
        return
    session_id = _conversation_for(mapping, role.workdir)
    if not session_id:
        return
    if not CONVERSATION_ID_RE.fullmatch(session_id):
        _skip(role, session_dir, f"agy's conversation map {path} names {session_id!r} for {role.workdir}, "
                                 "which is not a conversation id")
        return
    set_aside = session_dir / f"{name}.superseded"
    try:
        set_aside_at = set_aside.lstat().st_ctime
    except OSError:
        set_aside_at = None
    if set_aside_at is not None:
        if written_at <= set_aside_at:
            _skip(role, session_dir, f"agy's map names {session_id} for {role.role}, but was last written before "
                                     f"{role.role}'s record was set aside; starting as that left it", session_id)
            return
        previous = _session_record_at_path_for_role(role, set_aside) or {}
        source = str(previous.get("source") or "")
        if not (source.startswith(AGY_HOOK_SOURCE_PREFIX) or source == SOURCE):
            _skip(role, session_dir, f"agy's map names {session_id} for {role.role}, but the record set aside "
                                     f"last came from {source or 'an unnamed source'}, not agy; a role new to "
                                     "agy does not inherit an older agy conversation", session_id)
            return
    from scripts.session_provenance import agy_workspaces

    # SYRD-638: one agy still holds must be filed under the role's worktree; one it lost is the preflight's to refuse.
    held = agy_conversation_store_exists(session_id, root=path.parent.parent)
    workspaces = agy_workspaces(path.parent.parent, session_id) if held else None
    own = Path(role.workdir).expanduser().resolve(strict=False)
    if held and not any(Path(w).resolve(strict=False) == own for w in workspaces or ()):
        _skip(role, session_dir, f"agy's map names {session_id} for {role.workdir}, but agy files that conversation "
                                 f"under {', '.join(workspaces) if workspaces else 'no workspace'}; it is not "
                                 f"{role.role}'s to resume", session_id)
        return
    launcher._write_private_json_atomic(session_dir / name, {
        "target": role.target, "session_id": session_id, "updated_at": time.time(), "source": SOURCE,
        "payload": {"workdir": role.workdir, "map": str(path), "map_written_at": written_at},
    })
    from scripts.role_sessions import _decided

    _decided(role, session_dir, "", "adopted_agy_map",
             f"team-launcher: {role.role} has no recorded session; agy last opened {session_id} in "
             f"{role.workdir} ({path}), so that is the one to resume if agy still has it (SYRD-622)", session_id)
