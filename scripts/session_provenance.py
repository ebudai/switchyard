"""A role resumes only a conversation its provider records as the role's own (SYRD-638).

On syrd, 2026-10-21, Inspector's pane came up fresh on agy, as the launcher
decided. Ninety minutes later a `/resume` in that pane opened Audit's agy
conversation, `f4e8f4e1`: agy lists every conversation in the account, and its
own summary of that one names `syrd-worktrees/audit` as its workspace. The
pane then showed Audit's SYRD-594 prompt and spent a request in Audit's
history. agy also remapped Inspector's worktree to that conversation in
`cache/last_conversations.json`, which SYRD-622 adopts as a role's record, and
any turn would have made the pane's hook record it. Nothing between a record
and a resume asked whose conversation it was: provider ids are not scoped to a
role, and a record's own target says only which pane wrote it.

So before a role resumes anything, the provider's own account of the
conversation is read, and the conversation must be the role's:

* **Runtime.** A record a hook of another runtime wrote (`codex.SessionStart`
  for a role now on agy, say) is not this runtime's conversation.
* **Workspace.** The conversation's workspace, as the provider stores it, is
  the role's worktree:
  - agy: the conversation's `WorkspaceURIs` in agy's `cache/conversation_metadata.json`, or,
    where that summary does not hold it (a cutover copies only the conversations), the
    workspace URIs in the conversation's own database;
  - Codex: the `cwd` of the rollout's `session_meta`;
  - Copilot: the `cwd` of the session's `session.start` event;
  - Claude: the `cwd` its transcript's entries carry;
  - Hermes keeps each role's sessions in that role's own home, so there is
    nothing to compare.

A conversation the provider files under another workspace is foreign. One the
provider holds no workspace for has no proof. Either way it is not resumed:
`judge` says why, `_resume_preflight_allows_attempt` refuses it, and at a start
`set_aside` moves the record to `<record>.foreign-<reason>` (never over an
earlier one), keeping it whole for whoever looks, and the resume history names
the conversation, its workspace and the role's. The role then starts fresh.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig

#: What each runtime's hooks put before the event in a record's `source`.
SOURCE_RUNTIMES = {"gemini": "agy", "agy": "agy", "claude": "claude", "codex": "codex", "copilot": "copilot",
                   "hermes": "hermes"}
AGY_METADATA = Path("cache") / "conversation_metadata.json"
#: How much of a provider file is read for its workspace.
MAX_BYTES = 8 << 20
FOREIGN_SUFFIX = ".foreign-"
#: The project store's directory of conversation owners: one file per conversation, naming the role it is.
OWNERS_DIR = "conversation-owners"
#: Per-role store files that can name a conversation: records, set-aside records, unverified resumes, histories.
_RECORD_MARKERS = (".json", ".json.superseded", ".json.foreign-", ".json.resume_timeout")
_HISTORY_SUFFIX = ".resume.log"
#: What the launcher logs when a start will be, or was, a fresh one.


@dataclass(frozen=True)
class Verdict:
    """Whether the recorded conversation is the role's to resume, and if not why, in a slug and in words."""

    ok: bool
    reason: str = ""
    detail: str = ""
    workspaces: tuple[str, ...] = ()
    #: How the conversation was shown to be the role's: "owner" or "workspace".
    proof: str = ""


def _same(left: str, right: str) -> bool:
    return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)


def _read_bounded(path: Path) -> bytes | None:
    """A provider file's bytes, read without following a link and only up to `MAX_BYTES`; None if it is not one."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return handle.read(MAX_BYTES)


def _first_json_line(path: Path) -> dict[str, Any] | None:
    raw = _read_bounded(path)
    if raw is None:
        return None
    for line in raw.splitlines():
        if line.strip():
            try:
                parsed = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _varint(blob: bytes, at: int) -> tuple[int | None, int]:
    value = shift = 0
    while at < len(blob) and shift < 64:
        byte = blob[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7
    return None, at


def _file_uris(blob: bytes, depth: int = 0) -> list[str]:
    """Every `file://` string in a protobuf message, its nested messages included; malformed input ends the walk."""
    found: list[str] = []
    at = 0
    while at < len(blob):
        key, at = _varint(blob, at)
        if key is None:
            return found
        wire = key & 7
        if wire == 0:
            _value, at = _varint(blob, at)
        elif wire in (1, 5):
            at += 8 if wire == 1 else 4
        elif wire == 2:
            length, at = _varint(blob, at)
            if length is None or at + length > len(blob):
                return found
            chunk, at = blob[at:at + length], at + length
            if chunk.startswith(b"file://"):
                try:
                    found.append(chunk.decode("utf-8"))
                except UnicodeDecodeError:
                    pass
            elif depth < 8:
                found.extend(_file_uris(chunk, depth + 1))
        else:
            return found
    return found


def _agy_database_workspaces(root: Path, session_id: str) -> tuple[str, ...] | None:
    """The workspaces the conversation's own database names, read-only; None when it names none."""
    database = root / "conversations" / f"{session_id}.db"
    try:
        if not stat.S_ISREG(database.lstat().st_mode):
            return None
        with sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True) as conn:
            rows = conn.execute("SELECT data FROM trajectory_metadata_blob").fetchall()
    except (OSError, sqlite3.Error):
        return None
    found = {unquote(urlparse(uri).path) for (data,) in rows if isinstance(data, bytes)
             for uri in _file_uris(data[:MAX_BYTES]) if urlparse(uri).path}
    return tuple(sorted(found)) or None


def agy_workspaces(root: Path, session_id: str) -> tuple[str, ...] | None:
    """The workspaces agy's own summary of the conversation names, else its database; None when neither does."""
    summary = _agy_summary_workspaces(root, session_id)
    return summary if summary is not None else _agy_database_workspaces(root, session_id)


def _agy_summary_workspaces(root: Path, session_id: str) -> tuple[str, ...] | None:
    raw = _read_bounded(root / AGY_METADATA)
    try:
        conversations = json.loads(raw.decode("utf-8")).get("conversations") if raw is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    entry = conversations.get(session_id) if isinstance(conversations, dict) else None
    summary = entry.get("summary") if isinstance(entry, dict) else None
    uris = summary.get("WorkspaceURIs") if isinstance(summary, dict) else None
    if not isinstance(uris, list):
        return None
    found = tuple(unquote(urlparse(uri).path) for uri in uris
                  if isinstance(uri, str) and urlparse(uri).scheme == "file" and urlparse(uri).path)
    return found or None


def codex_workspaces(rollout: Path) -> tuple[str, ...] | None:
    first = _first_json_line(rollout)
    payload = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return (cwd,) if isinstance(cwd, str) and cwd else None


def copilot_workspaces(transcript: Path) -> tuple[str, ...] | None:
    first = _first_json_line(transcript)
    data = first.get("data") if isinstance(first, dict) and first.get("type") == "session.start" else None
    context = data.get("context") if isinstance(data, dict) else None
    cwd = context.get("cwd") if isinstance(context, dict) else None
    return (cwd,) if isinstance(cwd, str) and cwd else None


def claude_workspaces(transcript: Path) -> tuple[str, ...] | None:
    """The directory Claude ran in, from the first transcript entry that says (its first lines may be bookkeeping)."""
    raw = _read_bounded(transcript)
    for line in (raw or b"").splitlines():
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        cwd = entry.get("cwd") if isinstance(entry, dict) else None
        if isinstance(cwd, str) and cwd:
            return (cwd,)
    return None


def _workspaces(role: RoleConfig, session_id: str, record: dict[str, Any] | None, session_dir: Path,
                runtime: str) -> tuple[bool, str, tuple[str, ...] | None]:
    """Whether the provider holds this conversation, where it files it, and the workspaces it names there.

    A conversation the provider does not hold is the store check's to refuse, in its own words.
    """
    from scripts import provider_resume as resume

    home = resume._home_from_session_dir(session_dir)
    if runtime == "agy":
        root = resume.agy_conversation_root_for_session_dir(session_dir)
        held = resume.agy_conversation_store_exists(session_id, root=root)
        return held, str(root / AGY_METADATA), agy_workspaces(root, session_id)
    if runtime == "codex":
        found, location = resume.codex_session_store_exists(session_id, session_dir=session_dir, record=record)
        return found, location, codex_workspaces(Path(location)) if found else None
    if runtime == "copilot":
        from scripts import copilot_runtime

        found, location = copilot_runtime.session_store_exists(home, session_id, role.env, role.workdir)
        return found, location, copilot_workspaces(Path(location)) if found else None
    found, location = resume.claude_session_store_exists(role, session_id, session_dir=session_dir, record=record)
    return found, location, claude_workspaces(Path(location)) if found else None


def _project_store(role: RoleConfig, session_dir: Path) -> Path:
    """The project's shared session store: the parent of `roles/<role>/` when roles are isolated, else the store."""
    from scripts.stranded_session_records import shared_session_dir

    return shared_session_dir(role, session_dir) or session_dir


def _id_is_a_name(session_id: str) -> bool:
    return bool(session_id) and "/" not in session_id and "\0" not in session_id and session_id not in (".", "..")


def owner_of(role: RoleConfig, session_dir: Path, session_id: str) -> str:
    """The role a conversation's owner record names, read without following a link; "" when it has none."""
    if not _id_is_a_name(session_id):
        return ""
    raw = _read_bounded(_project_store(role, session_dir) / OWNERS_DIR / session_id)
    try:
        owner = json.loads(raw.decode("utf-8")).get("role") if raw is not None else ""
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return ""
    return owner if isinstance(owner, str) else ""


def claim(role: RoleConfig, session_dir: Path, session_id: str, how: str) -> None:
    """Record the conversation as the role's, if no role has it yet: the first claim stands. Never fatal."""
    if not _id_is_a_name(session_id):
        return
    owners = _project_store(role, session_dir) / OWNERS_DIR
    try:
        os.mkdir(owners, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return
    try:
        if not stat.S_ISDIR(owners.lstat().st_mode):
            return  # a link where the directory should be is not followed
        fd = os.open(owners / session_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return  # claimed already, or not writable: the claim that stands is the one there
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": role.role, "target": role.target, "session_id": session_id, "how": how,
                                 "at": time.time()}, sort_keys=True) + "\n")


def _history(path: Path) -> list[dict[str, Any]]:
    raw = _read_bounded(path)
    entries = []
    for line in (raw or b"").splitlines():
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _other_stores(role: RoleConfig, session_dir: Path) -> list[Path]:
    """Every file in the project's stores that another role's pane writes: its records and its history."""
    from scripts.session_records import session_file_name

    own = session_file_name(role.target)
    found: list[Path] = []
    project = _project_store(role, session_dir)
    directories = [project, session_dir]
    roles_dir = project / "roles"
    try:
        directories += [entry for entry in roles_dir.iterdir() if entry.is_dir() and not entry.is_symlink()
                        and entry.name != role.role]
    except OSError:
        pass
    for directory in dict.fromkeys(directories):
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            name = path.name
            if name.startswith(own.removesuffix(".json")) or path.is_symlink():
                continue  # this role's own pane, or a link nobody's pane wrote
            if name.endswith(_HISTORY_SUFFIX) or any(marker in name for marker in _RECORD_MARKERS):
                found.append(path)
    return found


def named_elsewhere(role: RoleConfig, session_dir: Path, session_id: str) -> str:
    """Where another role's pane recorded this conversation, if anywhere; "" when no other role's store names it."""
    for path in _other_stores(role, session_dir):
        if path.name.endswith(_HISTORY_SUFFIX):
            entries = _history(path)
        else:
            raw = _read_bounded(path)
            try:
                entries = [json.loads(raw.decode("utf-8"))] if raw is not None else []
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("session_id") == session_id and entry.get("target") != role.target:
                return f"{entry.get('target') or 'another pane'} recorded it ({path})"
    return ""


def role_runtime(role: RoleConfig) -> str:
    """The runtime whose conversations the role resumes, as `SOURCE_RUNTIMES` names it; '' for one that resumes none here."""
    from scripts import provider_resume as resume

    if resume._uses_agy_conversation_resume(role):
        return "agy"
    if resume._uses_codex_resume(role):
        return "codex"
    if resume._uses_copilot_resume(role):
        return "copilot"
    if resume._uses_claude_resume(role):
        return "claude"
    return ""


def source_runtime(record: dict[str, Any] | None) -> str:
    """The runtime whose hook wrote a record, from its `source`; '' when it names none."""
    source = str((record or {}).get("source") or "")
    return SOURCE_RUNTIMES.get(source.split(".", 1)[0], "")


def judge(role: RoleConfig, session_id: str, record: dict[str, Any] | None, *, session_dir: Path) -> Verdict:
    """Whether the provider records this conversation as the role's: its runtime's, and in the role's worktree."""
    runtime = role_runtime(role)
    if not runtime:
        return Verdict(True)  # Hermes keeps each role's sessions in its own home; anything else resumes nothing
    source = str((record or {}).get("source") or "")
    wrote = source_runtime(record)
    if wrote and wrote != runtime:
        return Verdict(False, "runtime",
                       f"its record was written by {source}, a {wrote} hook, and {role.role} now runs {runtime}")
    held, location, workspaces = _workspaces(role, session_id, record, session_dir, runtime)
    if not held:
        return Verdict(True)
    owner = owner_of(role, session_dir, session_id)
    if owner and owner != role.role:
        return Verdict(False, "owner", f"it is {owner}'s: {_project_store(role, session_dir) / OWNERS_DIR / session_id}")
    if workspaces is None:
        return Verdict(False, "no-proof",
                       f"{runtime} records no workspace for it at {location}, so nothing shows it is {role.role}'s")
    if not any(_same(workspace, role.workdir) for workspace in workspaces):
        return Verdict(False, "workspace",
                       f"{runtime} files it under {', '.join(workspaces)} ({location}), not {role.role}'s worktree "
                       f"{role.workdir}", workspaces)
    if owner == role.role:
        # Claimed by this role: by the pane's own hook when its provider opened the conversation as new, or by an
        # earlier resume the launcher accepted.
        return Verdict(True, workspaces=workspaces, proof="owner")
    if elsewhere := named_elsewhere(role, session_dir, session_id):
        return Verdict(False, "other-role", f"{elsewhere}, and no record shows it is {role.role}'s", workspaces)
    peers = getattr(role, "workdir_peers", ())
    if peers:
        # Its workspace is also theirs, so it cannot say whose the conversation is: fail closed.
        return Verdict(False, "shared-worktree",
                       f"{role.role}'s worktree {role.workdir} is also {', '.join(peers)}'s, and no owner record "
                       f"shows it is {role.role}'s", workspaces)
    return Verdict(True, workspaces=workspaces, proof="workspace")


def refusal(role: RoleConfig, session_id: str, verdict: Verdict) -> str:
    return (f"team-launcher: recorded {session_id} is not {role.role}'s conversation: {verdict.detail}; "
            "starting fresh instead of resuming another role's history (SYRD-638)")


def set_aside(role: RoleConfig, session_dir: Path, verdict: Verdict) -> Path | None:
    """Move the role's record to `<record>.foreign-<reason>`, never over an earlier one; None if there was none."""
    from scripts.session_records import session_file_name

    path = session_dir / session_file_name(role.target)
    base = path.with_name(f"{path.name}{FOREIGN_SUFFIX}{verdict.reason}")
    for n in range(1000):
        destination = base if n == 0 else base.with_name(f"{base.name}.{n}")
        try:
            os.link(path, destination, follow_symlinks=False)
        except FileExistsError:
            continue
        except FileNotFoundError:
            return None
        os.unlink(path)
        return destination
    return None


def refuse_foreign(role: RoleConfig, session_id: str, session_dir: Path, entry: str) -> str:
    """At a start: the refusal if the recorded conversation is not the role's, with its record set aside; else ""."""
    from scripts.role_sessions import _decided
    from scripts.session_records import _session_record_for_role

    verdict = judge(role, session_id, _session_record_for_role(role, session_dir), session_dir=session_dir)
    if verdict.ok:
        if verdict.proof:
            claim(role, session_dir, session_id, verdict.proof)  # durable from now on, whatever the records become
        return ""
    moved = set_aside(role, session_dir, verdict)
    message = refusal(role, session_id, verdict) + (f"; its record is kept at {moved}" if moved else "")
    _decided(role, session_dir, entry, f"foreign_{verdict.reason.replace('-', '_')}", message, session_id)
    return message
