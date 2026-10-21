"""A provider's session, as the provider itself files it, for suites that stand its store in (SYRD-638).

A resume needs the provider's own record of where the conversation ran, and a
stand-in that wrote `{}` held none: the launcher now refuses such a session as
having no proof that it is the role's. Each stand-in writes instead the first
thing the provider writes, measured on this host:

- Codex 0.163.0: the rollout's `session_meta`, with the `cwd` it ran in;
- Claude Code 2.1.302: transcript entries carrying `cwd`;
- Copilot CLI 1.0.101: `events.jsonl`'s `session.start`, with `context.cwd`;
- agy 1.4.2: the conversation's database, and its summary in
  `cache/conversation_metadata.json` with its `WorkspaceURIs`.
"""

from __future__ import annotations

import json
from pathlib import Path


def session_line(cli: str, session_id: str, cwd: str) -> str:
    if cli == "codex":
        return json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}})
    if cli == "copilot":
        return json.dumps({"type": "session.start", "data": {"sessionId": session_id, "context": {"cwd": cwd}}})
    return json.dumps({"type": "user", "sessionId": session_id, "cwd": cwd})  # claude


def write_session(path: Path, cli: str, session_id: str, cwd: str) -> Path:
    """The session file at `path`, as `cli` writes it, run in `cwd`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_line(cli, session_id, cwd) + "\n", encoding="utf-8")
    return path


def agy_conversation(root: Path, session_id: str, cwd: str) -> Path:
    """agy's conversation `session_id` in its store `root`, opened in `cwd`."""
    database = root / "conversations" / f"{session_id}.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_text("{}\n", encoding="utf-8")
    metadata = root / "cache" / "conversation_metadata.json"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else {"conversations": {}}
    data["conversations"][session_id] = {"summary": {"ID": session_id, "WorkspaceURIs": [f"file://{cwd}"]}}
    metadata.write_text(json.dumps(data), encoding="utf-8")
    return database


def transcript_for(role) -> str:
    """The first line of a session file `role`'s CLI writes, run in the role's worktree."""
    cli = Path(str(role.cli[0])).name if role.cli else "claude"
    return session_line(cli, "", role.workdir) + "\n"


class owned_record:
    """A role's session record that is the role's own conversation, as the launcher has it after resuming it.

    Where every role shares one worktree (the example `pgu` configuration), a conversation's workspace cannot
    say whose it is, and the launcher resumes only one it holds an owner record for (SYRD-638). Writing the
    record through this also writes that owner record, in the project's store.
    """

    def __init__(self, session_dir: Path, role) -> None:
        from scripts.session_records import session_file_name

        self.path = session_dir / session_file_name(role.target)
        self.role = role
        store = session_dir.parent.parent if session_dir.parent.name == "roles" and session_dir.name == role.role \
            else session_dir
        self.owners = store / "conversation-owners"

    def write_text(self, text: str, **kwargs) -> int:
        written = self.path.write_text(text, **kwargs)
        session_id = str(json.loads(text).get("session_id") or "")
        if session_id:
            self.owners.mkdir(parents=True, exist_ok=True)
            (self.owners / session_id).write_text(json.dumps({"role": self.role.role, "session_id": session_id}))
        return written
