#!/usr/bin/env python3
"""SYRD-638: a role resumes only a conversation its provider records as the role's own.

Live syrd, 2026-10-21. Audit had moved from agy to Codex; Inspector was set to
agy and started fresh. A `/resume` in Inspector's pane then opened Audit's agy
conversation `f4e8f4e1`, which agy's own summary files under
`syrd-worktrees/audit`, and the pane showed Audit's old SYRD-594 prompt. agy
remapped Inspector's worktree to that conversation in
`cache/last_conversations.json`, and a turn would have made the hook record it
as Inspector's. Nothing between a record and a resume asked whose it was.

Everything here is the real launcher: `_start_role_session`,
`attach_role_to_slot`, `_resume_preflight_allows_attempt`, SYRD-569's stranded
record adoption and SYRD-622's agy-map adoption, over provider stores in a
test home shaped as each provider writes them (agy's conversation database and
its `cache/conversation_metadata.json` summary, Codex's rollout with its
`session_meta`, Copilot's `events.jsonl`, Claude's project directory). tmux is
stood in for, and refuses anything but has-session and new-session, so no
prompt can be typed. No live home is used: HOME is the test's own.
The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "868ce147a0a5a04d02931930695173c628a1ba36"  # main before SYRD-638
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL", "TMUX")
CHECKS = 0
AUDIT_CONVERSATION = "f4e8f4e1-377f-4630-b9b4-cde47677fff1"
INSPECTOR_CONVERSATION = "ac53bccd-9337-415d-b765-856d917063f6"


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000638"


def scenario(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    from contextlib import redirect_stderr
    from dataclasses import replace

    from scripts import team_launcher as launcher

    home = work / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    raw = json.loads((root / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
    raw["run_as_user"] = getpass.getuser()
    ops = next(r for r in raw["roles"] if r["role"] == "ops")
    for role in raw["roles"]:
        role["workdir"] = str(work / "worktrees" / role["role"])  # each role its own worktree, as on syrd
        if role["role"] == "inspector":  # agy, resuming with --conversation, as syrd's Inspector is
            role.update(cli=["agy"], resume_mode="flag", resume_flag="--conversation")
            role.pop("model", None)
        if role["role"] == "audit":  # moved from agy to Codex
            role.update({k: ops[k] for k in ("cli", "resume_mode", "resume_subcommand", "resume_flag") if k in ops})
            role.pop("model", None)
        if role["role"] == "research":  # Copilot
            role.update(cli=["copilot"], resume_mode="flag", resume_flag="--resume")
            role.pop("model", None)
        if role["role"] == "app":  # one slot free, for attach-role
            role["detached"] = True
            role.pop("slot", None)
            role.update(cli=["agy"], resume_mode="flag", resume_flag="--conversation")
            role.pop("model", None)
    config_path = work / "pgu.json"
    layout = root / "config" / "team-launcher" / raw["layout"]
    shutil.copy(layout, work / layout.name)
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    shared = home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
    state = work / "pane-state"

    def load():
        return replace(launcher.load_project_config("pgu", config_path), session_dir=shared, role_state_isolation=True,
                       desktop_access={"mode": "headless"})

    config = load()
    roles = {role.role: role for role in config.roles}
    agy_root = home / ".gemini" / "antigravity-cli"
    launcher.AGY_CONVERSATION_ROOT = agy_root  # set at import from the real HOME: never read the live store
    elsewhere = str(work / "worktrees" / "somebody-else")

    class Tmux:
        """tmux stood in for; anything that types into a pane, and any other command, is refused."""

        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.live: set[str] = set()

        def __call__(self, args, **_kwargs):
            args = [str(a) for a in args]
            self.calls.append(args)
            if args[:1] != ["tmux"] or args[1] in ("send-keys", "paste-buffer", "load-buffer", "set-buffer"):
                raise AssertionError(f"refused: {args}")
            if args[1] == "has-session":
                return subprocess.CompletedProcess(args, 0 if args[3] in self.live else 1, "", "")
            if args[1] == "new-session":
                self.live.add(args[args.index("-s") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")

        def started(self) -> list[str]:
            return [call[-1] for call in self.calls if call[1:2] == ["new-session"]]

    launcher.live_command_matches_role = lambda role, runner=None: True
    launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.3
    launcher.RESUME_STARTUP_POLL_SECONDS = 0.05

    def agy_summary(session_id: str, workspace: str | None) -> None:
        """agy's own summary of a conversation, as `cache/conversation_metadata.json` holds it."""
        path = agy_root / "cache" / "conversation_metadata.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text()) if path.exists() else {"conversations": {}}
        data["conversations"][session_id] = {"summary": {
            "ID": session_id, "Title": "", "WorkspaceURIs": None if workspace is None else [f"file://{workspace}"],
            "AppDataDir": "antigravity-cli", "ProjectID": "default-cli-project"}, "is_internal": False}
        path.write_text(json.dumps(data), encoding="utf-8")

    def converse(role, session_id: str, workspace: str | None = "own") -> Path:
        """The provider's store holds this conversation, filed under `workspace` ("own": the role's worktree)."""
        where = role.workdir if workspace == "own" else workspace
        cli = launcher._command_name(role.cli[0])
        if cli == "claude":
            path = launcher._claude_project_dir_for_workdir(where or role.workdir, home=home) / f"{session_id}.jsonl"
            body = json.dumps({"type": "user", "cwd": where, "sessionId": session_id})
        elif cli == "codex":
            path = home / ".codex" / "sessions" / "2026" / "10" / "21" / f"rollout-2026-10-21T05-30-00-{session_id}.jsonl"
            body = json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": where}} if where
                              else {"type": "session_meta", "payload": {"id": session_id}})
        elif cli == "copilot":
            path = home / ".copilot" / "session-state" / session_id / "events.jsonl"
            body = json.dumps({"type": "session.start", "data": {"sessionId": session_id, "context": {"cwd": where}}})
        else:
            path = agy_root / "conversations" / f"{session_id}.db"
            body = "{}"
            agy_summary(session_id, where)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + "\n", encoding="utf-8")
        return path

    def own_dir(role) -> Path:
        return launcher.role_session_dir(config, role)

    def record_path(role) -> Path:
        return own_dir(role) / launcher.session_file_name(role.target)

    def seed(role, session_id: str, source: str, payload: dict | None = None, *, at: Path | None = None) -> None:
        path = at or record_path(role)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"target": role.target, "session_id": session_id, "updated_at": time.time(),
                                     "source": source, "payload": payload}) + "\n")

    def reset(role) -> None:
        shutil.rmtree(own_dir(role), ignore_errors=True)
        own_dir(role).mkdir(parents=True)

    def history(role) -> list[dict]:
        path = own_dir(role) / f"{launcher.session_file_name(role.target).removesuffix('.json')}.resume.log"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def kept(role) -> dict:
        """The records the role's store holds, beyond the history: name -> (session id, mode)."""
        found = {}
        for path in sorted(own_dir(role).iterdir()):
            if path.name.endswith(".resume.log") or not path.name.startswith(launcher.session_file_name(role.target)):
                continue
            try:
                found[path.name.removeprefix(launcher.session_file_name(role.target))] = [
                    json.loads(path.read_text()).get("session_id"), oct(stat.S_IMODE(path.stat().st_mode))]
            except (OSError, json.JSONDecodeError):
                found[path.name] = ["?", ""]
        return found

    ids = [AUDIT_CONVERSATION, INSPECTOR_CONVERSATION] + [sid(n) for n in range(1, 99)]

    def start(role) -> dict:
        tmux = Tmux()
        err = io.StringIO()
        with redirect_stderr(err):
            code = launcher._start_role_session(role, session_dir=own_dir(role), pane_state_dir=state, prefer_resume=True,
                                                seed_source="syrd638.start", runner=tmux)
        argv = " ".join(tmux.started())
        return {"code": code, "starts": len(tmux.started()), "resumed": [s for s in ids if s in argv],
                "said": err.getvalue(), "events": [e["event"] for e in history(role)],
                "detail": [e.get("detail", "") for e in history(role)], "kept": kept(role),
                "verbs": sorted({c[1] for c in tmux.calls})}

    def agy_map(entries: dict, *, at: float) -> None:
        path = agy_root / "cache" / "last_conversations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries), encoding="utf-8")
        os.utime(path, (at, at))

    out: dict = {}
    inspector, audit, director, research, app = (roles[n] for n in ("inspector", "audit", "director", "research", "app"))
    converse(inspector, INSPECTOR_CONVERSATION)
    converse(inspector, AUDIT_CONVERSATION, workspace=audit.workdir)  # Audit's agy conversation, from its agy days

    # 1. Inspector's pane recorded Audit's conversation (a /resume there, then a turn).
    reset(inspector)
    seed(inspector, AUDIT_CONVERSATION, "gemini.PreInvocation", {"cwd": inspector.workdir})
    out["foreign_record"] = start(inspector)
    out["foreign_record"]["preflight_after"] = launcher._resume_preflight_allows_attempt(
        inspector, AUDIT_CONVERSATION, session_dir=own_dir(inspector))

    # 2. Inspector's own conversation resumes, then resumes again after a restart.
    reset(inspector)
    seed(inspector, INSPECTOR_CONVERSATION, "gemini.SessionStart",
         {"conversationId": INSPECTOR_CONVERSATION, "workspacePaths": [inspector.workdir]})
    out["own"] = start(inspector)
    owner_record = shared / "conversation-owners" / INSPECTOR_CONVERSATION
    out["own_claim"] = json.loads(owner_record.read_text()) if owner_record.is_file() else None
    out["own_again"] = start(inspector)

    # 3. No record; an agy record was set aside; agy's map now names Audit's conversation for Inspector's worktree.
    reset(inspector)
    seed(inspector, INSPECTOR_CONVERSATION, "gemini.SessionStart")
    launcher.clear_session_record_for_role(inspector, own_dir(inspector))
    aside = (own_dir(inspector) / f"{launcher.session_file_name(inspector.target)}.superseded").stat().st_ctime
    agy_map({inspector.workdir: AUDIT_CONVERSATION, audit.workdir: AUDIT_CONVERSATION}, at=aside + 60)
    out["map"] = start(inspector)
    # ... and a map naming Inspector's own conversation is still taken (SYRD-622).
    reset(inspector)
    seed(inspector, INSPECTOR_CONVERSATION, "gemini.SessionStart")
    launcher.clear_session_record_for_role(inspector, own_dir(inspector))
    aside = (own_dir(inspector) / f"{launcher.session_file_name(inspector.target)}.superseded").stat().st_ctime
    agy_map({inspector.workdir: INSPECTOR_CONVERSATION}, at=aside + 60)
    out["map_own"] = start(inspector)

    # 4. The legacy record in the project's shared store names Audit's conversation (SYRD-569 takes it back).
    reset(inspector)
    (agy_root / "cache" / "last_conversations.json").unlink()
    seed(inspector, AUDIT_CONVERSATION, "gemini.SessionStart", at=shared / launcher.session_file_name(inspector.target))
    out["legacy"] = start(inspector)
    out["legacy"]["shared_left"] = (shared / launcher.session_file_name(inspector.target)).exists()
    out["legacy_again"] = start(inspector)
    (shared / launcher.session_file_name(inspector.target)).unlink()

    # 5. A record another runtime's hook wrote; a conversation agy holds no workspace for.
    reset(inspector)
    seed(inspector, INSPECTOR_CONVERSATION, "codex.SessionStart")
    out["runtime"] = start(inspector)
    reset(inspector)
    converse(inspector, sid(5), workspace=None)
    seed(inspector, sid(5), "gemini.SessionStart")
    out["no_proof"] = start(inspector)

    # 6. Audit, now on Codex: its own rollout resumes; a rollout from another worktree does not.
    reset(audit)
    converse(audit, sid(6))
    seed(audit, sid(6), "codex.SessionStart")
    out["codex_own"] = start(audit)
    reset(audit)
    converse(audit, sid(7), workspace=elsewhere)
    seed(audit, sid(7), "codex.SessionStart")
    out["codex_foreign"] = start(audit)
    # ... and a record from Audit's agy days, with its runtime now Codex.
    reset(audit)
    seed(audit, AUDIT_CONVERSATION, "gemini.SessionStart")
    out["codex_after_agy"] = start(audit)

    # 7. Copilot and Claude, the same way.
    reset(research)
    converse(research, sid(8))
    seed(research, sid(8), "copilot.sessionStart")
    out["copilot_own"] = start(research)
    reset(research)
    converse(research, sid(9), workspace=elsewhere)
    seed(research, sid(9), "copilot.sessionStart")
    out["copilot_foreign"] = start(research)
    reset(director)
    converse(director, sid(10))
    seed(director, sid(10), "claude.SessionStart")
    out["claude_own"] = start(director)
    reset(director)
    transcript = converse(director, sid(11), workspace=elsewhere)
    seed(director, sid(11), "claude.SessionStart", {"transcript_path": str(transcript)})
    out["claude_foreign"] = start(director)

    # 8. attach-role with another role's conversation recorded: refused, set aside, nothing started.
    reset(app)
    seed(app, AUDIT_CONVERSATION, "gemini.PreInvocation")
    tmux = Tmux()
    err = io.StringIO()
    with redirect_stderr(err):
        try:
            code = launcher.attach_role_to_slot(load(), config_path=config_path, role_name="app", slot=4,
                                                session_dir=own_dir(app), pane_state_dir=state, runner=tmux,
                                                print_func=lambda *_: None)
        except SystemExit as exc:
            code = f"exit: {exc}"
    out["attach"] = {"code": code, "starts": len(tmux.started()), "events": [e["event"] for e in history(app)],
                     "kept": kept(app), "said": err.getvalue()}
    reset(app)
    converse(app, sid(12))
    seed(app, sid(12), "gemini.SessionStart")
    tmux = Tmux()
    with redirect_stderr(io.StringIO()):
        try:
            code = launcher.attach_role_to_slot(load(), config_path=config_path, role_name="app", slot=4,
                                                session_dir=own_dir(app), pane_state_dir=state, runner=tmux,
                                                print_func=lambda *_: None)
        except SystemExit as exc:
            code = f"exit: {exc}"
    out["attach_own"] = {"code": code, "resumed": any(sid(12) in call for call in tmux.started())}

    # 9. After a cutover, which copies agy's conversations but not its summary: the database itself says.
    import sqlite3

    def agy_database_only(session_id: str, workspace: str) -> None:
        uri = f"file://{workspace}".encode()
        inner = b"\x0a" + bytes([len(uri)]) + uri  # a nested message holding the workspace URI, as agy 1.4.2 writes it
        database = agy_root / "conversations" / f"{session_id}.db"
        database.unlink(missing_ok=True)
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE trajectory_metadata_blob (id text DEFAULT 'main', data blob, PRIMARY KEY (id))")
            conn.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (b"\x08\x01\x12" + bytes([len(inner)]) + inner,))
    reset(inspector)
    agy_database_only(sid(13), inspector.workdir)
    seed(inspector, sid(13), "gemini.SessionStart")
    out["cutover_own"] = start(inspector)
    reset(inspector)
    agy_database_only(sid(14), audit.workdir)
    seed(inspector, sid(14), "gemini.SessionStart")
    out["cutover_foreign"] = start(inspector)

    # 10. A summary that is a link -- something planted in agy's store vouching for a conversation -- is not read.
    reset(inspector)
    agy_database_only(sid(15), elsewhere)
    metadata = agy_root / "cache" / "conversation_metadata.json"
    planted = work / "planted.json"
    planted.write_text(json.dumps({"conversations": {sid(15): {"summary": {"WorkspaceURIs": [f"file://{inspector.workdir}"]}}}}))
    real = metadata.read_bytes()
    metadata.unlink()
    metadata.symlink_to(planted)
    seed(inspector, sid(15), "gemini.SessionStart")
    out["linked_summary"] = start(inspector)
    metadata.unlink()
    metadata.write_bytes(real)

    # 11. A conversation the provider no longer has is still the store check's to refuse, in its own words.
    reset(inspector)
    seed(inspector, sid(16), "gemini.SessionStart")
    out["gone"] = start(inspector)

    # 12. A second refusal never overwrites the first record set aside.
    reset(inspector)
    for _ in range(2):
        seed(inspector, AUDIT_CONVERSATION, "gemini.PreInvocation")
        start(inspector)
    out["twice"] = kept(inspector)

    # 13. pgu's example configuration: every role in one worktree, so a workspace cannot say whose a conversation
    #     is. Audit and Inspector both on agy.
    raw2 = json.loads(json.dumps(raw))
    for entry in raw2["roles"]:
        entry.pop("workdir", None)
        if entry["role"] == "audit":
            entry.update(cli=["agy"], resume_mode="flag", resume_flag="--conversation")
            entry.pop("resume_subcommand", None)
    config2_path = work / "pgu-shared.json"
    config2_path.write_text(json.dumps(raw2), encoding="utf-8")
    shared2 = home / ".local" / "state" / "pgu-shared-ticket-board" / "pane-sessions"
    config2 = replace(launcher.load_project_config("pgu", config2_path), session_dir=shared2, role_state_isolation=True,
                      desktop_access={"mode": "headless"})
    roles2 = {role.role: role for role in config2.roles}
    s_audit, s_inspector = roles2["audit"], roles2["inspector"]
    s_director = roles2["director"]
    own_dir_by_role = {"audit": launcher.role_session_dir(config2, s_audit),
                       "inspector": launcher.role_session_dir(config2, s_inspector),
                       "director": launcher.role_session_dir(config2, s_director)}

    def own2(role) -> Path:
        return own_dir_by_role[role.role]

    def seed2(role, session_id: str, source: str) -> None:
        own2(role).mkdir(parents=True, exist_ok=True)
        seed(role, session_id, source, at=own2(role) / launcher.session_file_name(role.target))

    def log2(role, *entries: dict) -> None:
        path = own2(role) / f"{launcher.session_file_name(role.target).removesuffix('.json')}.resume.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            for entry in entries:
                handle.write(json.dumps({"target": role.target, "time": time.time(), **entry}) + "\n")

    def start2(role) -> dict:
        tmux = Tmux()
        err = io.StringIO()
        with redirect_stderr(err):
            code = launcher._start_role_session(role, session_dir=own2(role), pane_state_dir=state, prefer_resume=True,
                                                seed_source="syrd638.shared", runner=tmux)
        argv = " ".join(tmux.started())
        log = own2(role) / f"{launcher.session_file_name(role.target).removesuffix('.json')}.resume.log"
        events = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        owners = shared2 / "conversation-owners"
        return {"code": code, "resumed": [s for s in ids if s in argv],
                "argv_has": {s: s in argv for s in (sid(20), sid(21), sid(22), sid(23), sid(28), sid(29))},
                "events": [e["event"] for e in events if e.get("by") == "launcher"],
                "detail": [e.get("detail", "") for e in events if e.get("by") == "launcher"],
                "kept": {path.name.removeprefix(launcher.session_file_name(role.target)): json.loads(path.read_text())["session_id"]
                         for path in sorted(own2(role).glob(launcher.session_file_name(role.target) + "*"))},
                "owners": {path.name: json.loads(path.read_text())["role"] for path in sorted(owners.iterdir())}
                          if owners.is_dir() else {},
                "claimed_by": {path.name: json.loads(path.read_text()).get("how") for path in sorted(owners.iterdir())}
                              if owners.is_dir() else {}}

    out["shared_peers"] = {"audit": list(getattr(s_audit, "workdir_peers", ())),
                           "inspector": list(getattr(s_inspector, "workdir_peers", ())),
                           "same_worktree": s_audit.workdir == s_inspector.workdir}
    for n in range(20, 24):
        converse(s_inspector, sid(n), workspace=s_inspector.workdir)  # every one filed under the one worktree
    # a. Audit's own store holds its conversation; Inspector's pane recorded it after a /resume.
    seed2(s_audit, sid(20), "gemini.SessionStart")
    seed2(s_inspector, sid(20), "gemini.PreInvocation")
    out["shared_traced"] = start2(s_inspector)
    # b. Nothing left of it in Audit's store, and nobody owns it: the workspace cannot tell, so nothing is resumed.
    seed2(s_inspector, sid(21), "gemini.PreInvocation")
    out["shared_untraced"] = start2(s_inspector)
    # c. Audit owns it, though its own records have moved on.
    (shared2 / "conversation-owners").mkdir(parents=True, exist_ok=True)
    (shared2 / "conversation-owners" / sid(22)).write_text(json.dumps({"role": "audit", "session_id": sid(22)}))
    seed2(s_inspector, sid(22), "gemini.PreInvocation")
    out["shared_owned_elsewhere"] = start2(s_inspector)
    def hook(role, source: str, session_id: str, started: str = "") -> None:
        """The installed pane hook, run as the role's pane runs it, with the payload its provider sends."""
        body = {"session_id": session_id, "hook_event_name": source.split(".", 1)[1], "cwd": role.workdir}
        if started:
            body["source"] = started
        subprocess.run([sys.executable, str(root / "scripts" / "ticket-board-pane-idle-hook"), "idle", "--source", source,
                        "--state-dir", str(state), "--session-dir", str(own2(role)), "--target", role.target,
                        *(["--record-session"] if source.endswith("SessionStart") else [])],
                       input=json.dumps(body), text=True, capture_output=True, timeout=60, check=False,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(home), "TICKET_BOARD_PANE_TARGET": role.target,
                            "TICKET_BOARD_PANE_SESSION_DIR": str(own2(role)), "TICKET_BOARD_CALLER_ROLE": role.role})

    def fresh(role) -> None:
        """A fresh start: the launcher sets the role's record aside before the provider starts."""
        launcher.clear_session_record_for_role(role, own2(role))
        log2(role, {"by": "launcher", "event": "fresh_started", "session_id": ""})

    # d. Inspector's own: its agy opens a new conversation at a fresh start, and the pane's hook says so.
    converse(s_inspector, sid(23), workspace=s_inspector.workdir)
    own2(s_inspector).mkdir(parents=True, exist_ok=True)
    fresh(s_inspector)
    hook(s_inspector, "gemini.SessionStart", sid(23))
    out["shared_own"] = start2(s_inspector)
    out["shared_own_again"] = start2(s_inspector)
    # e. Owned, with nothing left in the history: the owner record alone is the proof.
    converse(s_inspector, sid(24), workspace=s_inspector.workdir)
    (shared2 / "conversation-owners" / sid(24)).write_text(json.dumps({"role": "inspector", "session_id": sid(24)}))
    seed2(s_inspector, sid(24), "gemini.SessionStart")
    out["shared_owned"] = start2(s_inspector)
    # h. Audit's sequence: a fresh start records nothing, then a /resume, and the first turn's PreInvocation names
    #    another role's conversation, with nothing recorded before it.
    converse(s_inspector, sid(28), workspace=s_inspector.workdir)
    fresh(s_inspector)
    hook(s_inspector, "gemini.PreInvocation", sid(28))
    out["shared_resume_first"] = start2(s_inspector)
    # i. Claude: Audit opens a conversation after the Director's fresh start, and the Director's pane /resumes it
    #    before recording its own; its first recorded event is that conversation's SessionStart, `resume`.
    converse(s_director, sid(29), workspace=s_director.workdir)
    own2(s_director).mkdir(parents=True, exist_ok=True)
    fresh(s_director)
    hook(s_director, "claude.SessionStart", sid(29), "resume")
    out["shared_claude_resumed"] = start2(s_director)
    # ... and a conversation Claude opens at a fresh start, `startup`, is the Director's.
    converse(s_director, sid(30), workspace=s_director.workdir)
    fresh(s_director)
    hook(s_director, "claude.SessionStart", sid(30), "startup")
    out["shared_claude_new"] = start2(s_director)

    # g. The first claim stands (a tree before SYRD-638 has no claims to make).
    try:
        from scripts import session_provenance
    except ImportError:
        session_provenance = None
    if session_provenance is not None:
        session_provenance.claim(s_audit, own2(s_audit), sid(27), "test")
        session_provenance.claim(s_inspector, own2(s_inspector), sid(27), "test")
        out["first_claim"] = json.loads((shared2 / "conversation-owners" / sid(27)).read_text())["role"]
    out["home"] = str(home)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd638.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), tmp],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def candidate(out: dict) -> None:
    foreign = out["foreign_record"]
    detail = foreign["detail"][0]
    check(foreign["code"] == 0 and foreign["starts"] == 1 and foreign["resumed"] == []
          and foreign["events"] == ["foreign_workspace", "fresh_started"],
          f"Inspector's record of Audit's conversation is not resumed: Inspector starts fresh: {foreign}")
    check(f"recorded {AUDIT_CONVERSATION} is not inspector's conversation: agy files it under " in detail
          and "/worktrees/audit (" in detail and "not inspector's worktree " in detail and "/worktrees/inspector;" in detail
          and "pgu-inspector_0.0.json.foreign-workspace" in detail and detail in foreign["said"],
          f"the history and the start's own output name the conversation, agy's workspace for it and Inspector's: {detail}")
    check(foreign["kept"] == {".foreign-workspace": [AUDIT_CONVERSATION, "0o600"]},
          f"the record is kept whole and private as `.foreign-workspace`, and is not the record any more: {foreign['kept']}")
    check(set(foreign["verbs"]) <= {"has-session", "new-session", "set-option"},
          f"nothing is typed into the pane: {foreign['verbs']}")
    check(foreign["preflight_after"][0] is False and "is not inspector's conversation" in foreign["preflight_after"][1],
          f"the resume preflight refuses it too, as any caller asks it: {foreign['preflight_after']}")
    check(out["own"]["resumed"] == [INSPECTOR_CONVERSATION] and out["own"]["events"] == ["resumed"]
          and out["own_again"]["resumed"] == [INSPECTOR_CONVERSATION] and out["own_again"]["events"] == ["resumed", "resumed"],
          f"Inspector's own conversation resumes, and again after a restart: {out['own']} {out['own_again']}")
    claim = out["own_claim"] or {}
    check(claim.get("role") == "inspector" and claim.get("how") == "workspace",
          f"the resume the launcher accepted claims the conversation for Inspector, from here on: {out['own_claim']}")
    mapped = out["map"]
    check(mapped["resumed"] == [] and mapped["events"] == ["agy_map_not_taken", "no_record", "fresh_started"]
          and "agy files that conversation under " in mapped["detail"][0] and "/worktrees/audit;" in mapped["detail"][0]
          and "it is not inspector's to resume" in mapped["detail"][0] and "" not in mapped["kept"],
          f"agy's map naming Audit's conversation for Inspector's worktree is not adopted, and no record is written: {mapped}")
    check(out["map_own"]["resumed"] == [INSPECTOR_CONVERSATION] and out["map_own"]["events"] == ["adopted_agy_map", "resumed"],
          f"a map naming Inspector's own conversation still is (SYRD-622): {out['map_own']}")
    legacy = out["legacy"]
    check(legacy["resumed"] == [] and legacy["events"] == ["adopted_shared", "foreign_workspace", "fresh_started"]
          and legacy["shared_left"] and legacy["kept"] == {".foreign-workspace": [AUDIT_CONVERSATION, "0o600"]},
          f"a legacy shared record naming Audit's conversation is taken back, refused and set aside, the shared one left: {legacy}")
    again = out["legacy_again"]
    check(again["resumed"] == [] and again["events"][3:] == ["no_record", "fresh_started"] and again["kept"] == legacy["kept"],
          f"and the next start does not take it back again: {again}")
    check(out["runtime"]["resumed"] == [] and out["runtime"]["events"] == ["foreign_runtime", "fresh_started"]
          and "written by codex.SessionStart, a codex hook, and inspector now runs agy" in out["runtime"]["detail"][0]
          and out["runtime"]["kept"] == {".foreign-runtime": [INSPECTOR_CONVERSATION, "0o600"]},
          f"a record another runtime's hook wrote is not handed to agy: {out['runtime']}")
    check(out["no_proof"]["resumed"] == [] and out["no_proof"]["events"] == ["foreign_no_proof", "fresh_started"]
          and "agy records no workspace for it" in out["no_proof"]["detail"][0]
          and out["no_proof"]["kept"] == {".foreign-no-proof": [sid(5), "0o600"]},
          f"a conversation agy holds no workspace for is not resumed, and kept as `.foreign-no-proof`: {out['no_proof']}")
    check(out["codex_own"]["resumed"] == [sid(6)] and out["codex_own"]["events"] == ["resumed"]
          and out["codex_foreign"]["resumed"] == [] and out["codex_foreign"]["events"] == ["foreign_workspace", "fresh_started"]
          and "codex files it under " in out["codex_foreign"]["detail"][0]
          and out["codex_after_agy"]["resumed"] == [] and out["codex_after_agy"]["events"] == ["foreign_runtime", "fresh_started"],
          f"Codex: Audit's own rollout resumes; another worktree's, or Audit's record from its agy days, does not: "
          f"{out['codex_own']} {out['codex_foreign']} {out['codex_after_agy']}")
    check(out["copilot_own"]["resumed"] == [sid(8)] and out["copilot_foreign"]["resumed"] == []
          and out["copilot_foreign"]["events"] == ["foreign_workspace", "fresh_started"],
          f"Copilot the same: {out['copilot_own']} {out['copilot_foreign']}")
    check(out["claude_own"]["resumed"] == [sid(10)] and out["claude_foreign"]["resumed"] == []
          and out["claude_foreign"]["events"] == ["foreign_workspace", "fresh_started"],
          f"Claude the same: {out['claude_own']} {out['claude_foreign']}")
    attach = out["attach"]
    check(attach["code"] == 1 and attach["starts"] == 0 and attach["events"] == ["foreign_workspace"]
          and attach["kept"] == {".foreign-workspace": [AUDIT_CONVERSATION, "0o600"]},
          f"attach-role refuses another role's conversation, sets it aside and starts nothing: {attach}")
    check(out["attach_own"]["code"] == 0 and out["attach_own"]["resumed"],
          f"and attaches a role with its own: {out['attach_own']}")
    check(out["cutover_own"]["resumed"] == [sid(13)] and out["cutover_foreign"]["resumed"] == []
          and out["cutover_foreign"]["events"] == ["foreign_workspace", "fresh_started"]
          and "/worktrees/audit (" in out["cutover_foreign"]["detail"][0],
          f"with no summary for it, as after a cutover, agy's conversation database names its workspace: "
          f"{out['cutover_own']} {out['cutover_foreign']}")
    check(out["linked_summary"]["resumed"] == [] and out["linked_summary"]["events"] == ["foreign_workspace", "fresh_started"],
          f"a summary that is a link is not read: the conversation's own database decides: {out['linked_summary']}")
    gone = out["gone"]
    check(gone["resumed"] == [] and gone["events"] == ["preflight_refused", "fresh_started"]
          and "is not present in the local Antigravity store" in gone["detail"][0]
          and gone["kept"] == {".superseded": [sid(16), "0o600"]},
          f"a conversation agy no longer holds is refused as before, and set aside as before: {gone}")
    check(out["twice"] == {".foreign-workspace": [AUDIT_CONVERSATION, "0o600"],
                           ".foreign-workspace.1": [AUDIT_CONVERSATION, "0o600"]},
          f"a second refusal keeps the first record set aside: {out['twice']}")


def shared(out: dict) -> None:
    check(out["shared_peers"]["same_worktree"] and "inspector" in out["shared_peers"]["audit"]
          and "audit" in out["shared_peers"]["inspector"],
          f"in pgu's example configuration every role shares one worktree, and each knows its peers: {out['shared_peers']}")
    traced, untraced, owned = out["shared_traced"], out["shared_untraced"], out["shared_owned_elsewhere"]
    check(traced["resumed"] == [] and not traced["argv_has"][sid(20)] and traced["events"][-2:] == ["foreign_other_role", "fresh_started"]
          and "pgu-audit:0.0 recorded it" in traced["detail"][-2] and traced["kept"].get(".foreign-other-role") == sid(20),
          f"a shared worktree: Audit's conversation, recorded in Audit's store, is not handed to Inspector's agy, and is kept "
          f"as `.foreign-other-role`: {traced}")
    check(untraced["resumed"] == [] and not untraced["argv_has"][sid(21)]
          and untraced["events"][-2:] == ["foreign_shared_worktree", "fresh_started"]
          and "is also " in untraced["detail"][-2] and "no owner record shows it is inspector's" in untraced["detail"][-2]
          and untraced["kept"].get(".foreign-shared-worktree") == sid(21),
          f"with no trace of whose it is, a shared worktree fails closed, and the record is kept with its reason: {untraced}")
    check(owned["resumed"] == [] and not owned["argv_has"][sid(22)] and owned["events"][-2:] == ["foreign_owner", "fresh_started"]
          and "it is audit's" in owned["detail"][-2] and owned["kept"].get(".foreign-owner") == sid(22),
          f"a conversation Audit owns is never Inspector's, whatever Audit's records now say: {owned}")
    own, again = out["shared_own"], out["shared_own_again"]
    check(own["resumed"] == [sid(23)] and own["owners"].get(sid(23)) == "inspector"
          and own["claimed_by"].get(sid(23)) == "hook:gemini.SessionStart"
          and again["resumed"] == [sid(23)] and again["owners"].get(sid(23)) == "inspector",
          f"Inspector's own conversation, begun in its pane, resumes in the shared worktree, is claimed, and resumes again: "
          f"{own} {again}")
    check(out["shared_owned"]["resumed"] == [sid(24)],
          f"the role's own owner record alone lets its conversation resume in the shared worktree: {out['shared_owned']}")
    first = out["shared_resume_first"]
    check(first["resumed"] == [] and not first["argv_has"][sid(28)]
          and first["events"][-2:] == ["foreign_shared_worktree", "fresh_started"] and sid(28) not in first["owners"]
          and any(k.startswith(".foreign-shared-worktree") and v == sid(28) for k, v in first["kept"].items()),
          f"a /resume before the fresh session was recorded: the first PreInvocation's conversation is not handed to "
          f"agy, not claimed, and kept with its reason: {first}")
    resumed_claude, new_claude = out["shared_claude_resumed"], out["shared_claude_new"]
    check(resumed_claude["resumed"] == [] and not resumed_claude["argv_has"][sid(29)] and sid(29) not in resumed_claude["owners"]
          and resumed_claude["events"][-2:] == ["foreign_shared_worktree", "fresh_started"]
          and resumed_claude["kept"].get(".foreign-shared-worktree") == sid(29),
          f"a conversation opened after the fresh start but /resumed into the pane (SessionStart `resume`) is not "
          f"handed to Claude, not claimed, and kept with its reason: {resumed_claude}")
    check(new_claude["resumed"] == [sid(30)] and new_claude["owners"].get(sid(30)) == "director"
          and new_claude["claimed_by"].get(sid(30)) == "hook:claude.SessionStart" and sid(29) not in new_claude["owners"],
          f"a conversation Claude opens at a fresh start (SessionStart `startup`) is claimed by the pane's hook and "
          f"resumes: {new_claude}")
    check(out["first_claim"] == "audit", f"a second claim never replaces the first: {out['first_claim']}")
    check(sid(20) not in own["owners"] and sid(21) not in own["owners"],
          f"no refused conversation is ever claimed: {own['owners']}")


def before(out: dict) -> None:
    resumed = {case: out[case]["resumed"] for case in ("foreign_record", "map", "legacy", "runtime", "no_proof",
                                                      "codex_foreign", "copilot_foreign", "claude_foreign")}
    check(resumed == {"foreign_record": [AUDIT_CONVERSATION], "map": [AUDIT_CONVERSATION], "legacy": [AUDIT_CONVERSATION],
                      "runtime": [INSPECTOR_CONVERSATION], "no_proof": [sid(5)], "codex_foreign": [sid(7)],
                      "copilot_foreign": [sid(9)], "claude_foreign": [sid(11)]},
          f"before: each role resumes whatever conversation it holds a record or a map entry for: {resumed}")
    shared_resumed = [out[c]["resumed"] for c in ("shared_traced", "shared_untraced", "shared_owned_elsewhere",
                                                    "shared_resume_first", "shared_claude_resumed")]
    check(shared_resumed == [[sid(20)], [sid(21)], [sid(22)], [sid(28)], [sid(29)]],
          f"before: in a shared worktree Inspector resumes each of Audit's conversations: {shared_resumed}")
    check(out["attach"]["code"] == 0 and out["attach"]["starts"] == 1 and out["attach"]["events"] == ["resumed"],
          f"before: attach-role starts Audit's conversation in App's pane: {out['attach']}")
    check(out["own"]["resumed"] == [INSPECTOR_CONVERSATION] and out["codex_own"]["resumed"] == [sid(6)],
          f"before: and a role's own conversation resumes, as it still does: {out['own']} {out['codex_own']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD638_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd638-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    shared(after)
    print(f"session_provenance_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
