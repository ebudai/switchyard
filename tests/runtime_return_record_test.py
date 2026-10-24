#!/usr/bin/env python3
"""SYRD-622 (2026-10-24 recurrence): a role back on a runtime it left resumes the conversation it left there.

Live on syrd: Inspector ran agy on its own conversation `ac53bccd`, filed by
agy under its own worktree. On 2026-10-21 the Director switched it to Copilot
for SYRD-634's acceptance, and back to agy that night. The switch set the agy
record aside; Copilot's records were set aside after it, and the store keeps
two set-asides, so by the switch back nothing remembered `ac53bccd`. The next
two launches, 10-23 and 10-24, started agy fresh (`no_record`), after
declining agy's map, which still named Audit's `f4e8f4e1` for Inspector's
worktree; the User then resumed by hand.

That sequence is replayed here through the real switch step
(`role_runtime._restart_worker` on a stopped role, which sets the record aside
as the live switch did) and the real starts (`_start_role_session`,
`attach_role_to_slot`), over provider stores in a test home shaped as each
provider writes them. tmux is stood in for and refuses anything that types
into a pane; no live home or store is read. The before-run is main's code,
from a git archive.
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
BEFORE = "d24a15cf06d09e5282247caf2f77bff0614c9460"  # main before this fix
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL", "TMUX")
CHECKS = 0
AUDIT_CONVERSATION = "f4e8f4e1-377f-4630-b9b4-cde47677fff1"
INSPECTOR_CONVERSATION = "ac53bccd-9337-415d-b765-856d917063f6"
RUNTIMES = {"agy": {"cli": ["agy"], "resume_mode": "flag", "resume_flag": "--conversation"},
            "copilot": {"cli": ["copilot"], "resume_mode": "flag", "resume_flag": "--resume"},
            "claude": {"cli": ["claude"], "resume_mode": "flag", "resume_flag": "--resume"}}
HOOK_SOURCES = {"agy": "gemini.SessionStart", "copilot": "copilot.sessionStart", "claude": "claude.SessionStart"}


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000622"


def scenario(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    from contextlib import redirect_stderr
    from dataclasses import replace

    from scripts import role_runtime
    from scripts import team_launcher as launcher

    home = work / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    raw = json.loads((root / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
    raw["run_as_user"] = getpass.getuser()
    for role in raw["roles"]:
        role["workdir"] = str(work / "worktrees" / role["role"])  # each role its own worktree, as on syrd
        if role["role"] == "app":  # one slot free, for attach-role
            role["detached"] = True
            role.pop("slot", None)
    layout = root / "config" / "team-launcher" / raw["layout"]
    shutil.copy(layout, work / layout.name)
    shared = home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
    state = work / "pane-state"
    runtimes = {"inspector": "agy", "app": "agy", "audit": "claude"}

    def load():
        """The configuration as it stands, each of the roles above on its runtime."""
        for role in raw["roles"]:
            if role["role"] in runtimes:
                for key in ("cli", "resume_mode", "resume_subcommand", "resume_flag", "model"):
                    role.pop(key, None)
                role.update(RUNTIMES[runtimes[role["role"]]])
        path = work / "pgu.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return replace(launcher.load_project_config("pgu", path), session_dir=shared, role_state_isolation=True,
                       desktop_access={"mode": "headless"}), path

    def role_now(name: str):
        config, _ = load()
        return next(r for r in config.roles if r.role == name)

    agy_root = home / ".gemini" / "antigravity-cli"
    launcher.AGY_CONVERSATION_ROOT = agy_root  # set at import from the real HOME: never read the live store

    class Tmux:
        """tmux stood in for; anything that types into a pane, and any other command, is refused."""

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, args, **_kwargs):
            args = [str(a) for a in args]
            self.calls.append(args)
            if args[:1] != ["tmux"] or args[1] in ("send-keys", "paste-buffer", "load-buffer", "set-buffer"):
                raise AssertionError(f"refused: {args}")
            if args[1] == "has-session":
                return subprocess.CompletedProcess(args, 1, "", "")  # stopped: started here, never live before
            return subprocess.CompletedProcess(args, 0, "", "")

        def started(self) -> list[str]:
            return [call[-1] for call in self.calls if call[1:2] == ["new-session"]]

    launcher.live_command_matches_role = lambda role, runner=None: True
    launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.3
    launcher.RESUME_STARTUP_POLL_SECONDS = 0.05

    def converse(role, runtime: str, session_id: str, workspace: str | None = None) -> None:
        """The provider's store holds this conversation, filed under `workspace` (by default the role's worktree)."""
        where = workspace or role.workdir
        if runtime == "claude":
            path = launcher._claude_project_dir_for_workdir(where, home=home) / f"{session_id}.jsonl"
            body = json.dumps({"type": "user", "cwd": where, "sessionId": session_id})
        elif runtime == "copilot":
            path = home / ".copilot" / "session-state" / session_id / "events.jsonl"
            body = json.dumps({"type": "session.start", "data": {"sessionId": session_id, "context": {"cwd": where}}})
        else:
            path = agy_root / "conversations" / f"{session_id}.db"
            body = "{}"
            summary = agy_root / "cache" / "conversation_metadata.json"
            summary.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(summary.read_text()) if summary.exists() else {"conversations": {}}
            data["conversations"][session_id] = {"summary": {"ID": session_id, "WorkspaceURIs": [f"file://{where}"]}}
            summary.write_text(json.dumps(data), encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + "\n", encoding="utf-8")

    def own_dir(role) -> Path:
        return launcher.role_session_dir(load()[0], role)

    def name(role) -> str:
        return launcher.session_file_name(role.target)

    def recorded(role, runtime: str, session_id: str) -> None:
        """The pane's hook records the conversation its provider opened, as each runtime's SessionStart does."""
        converse(role, runtime, session_id)
        path = own_dir(role) / name(role)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"target": role.target, "session_id": session_id, "updated_at": time.time(),
                                     "source": HOOK_SOURCES[runtime], "payload": {"cwd": role.workdir}}) + "\n")

    def history(role) -> list[dict]:
        path = own_dir(role) / f"{name(role).removesuffix('.json')}.resume.log"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def kept(role) -> dict:
        """The records the role's store holds, beyond the history: suffix -> [session id, mode]."""
        found = {}
        for path in sorted(own_dir(role).iterdir()):
            if path.name.endswith(".resume.log") or not path.name.startswith(name(role)):
                continue
            try:
                found[path.name.removeprefix(name(role))] = [json.loads(path.read_text()).get("session_id"),
                                                            oct(stat.S_IMODE(path.lstat().st_mode))]
            except (OSError, json.JSONDecodeError):
                found[path.name.removeprefix(name(role))] = ["?", ""]
        return found

    def switch(role_name: str, runtime: str) -> None:
        """The Director's set-role-runtime on a stopped role: the configuration changes, then the switch's own step."""
        runtimes[role_name] = runtime
        config, _ = load()
        with redirect_stderr(io.StringIO()):
            role_runtime._restart_worker(config, role_name=role_name, pane_state_dir=state, runner=Tmux(),
                                         print_func=lambda *_: None)

    ids = [AUDIT_CONVERSATION, INSPECTOR_CONVERSATION] + [sid(n) for n in range(1, 60)]
    mark = {"n": 0}

    def start(role_name: str, *, prefer_resume: bool = True) -> dict:
        role = role_now(role_name)
        before = len(history(role))
        tmux = Tmux()
        err = io.StringIO()
        with redirect_stderr(err):
            code = launcher._start_role_session(role, session_dir=own_dir(role), pane_state_dir=state,
                                                prefer_resume=prefer_resume, seed_source="syrd622.start", runner=tmux)
        argv = " ".join(tmux.started())
        new = history(role)[before:]
        return {"code": code, "resumed": [s for s in ids if s in argv], "events": [e["event"] for e in new],
                "detail": [e.get("detail", "") for e in new], "kept": kept(role),
                "verbs": sorted({c[1] for c in tmux.calls})}

    def reset(role_name: str, runtime: str) -> None:
        runtimes[role_name] = runtime
        shutil.rmtree(own_dir(role_now(role_name)), ignore_errors=True)
        own_dir(role_now(role_name)).mkdir(parents=True)

    def agy_map(entries: dict, *, at: float) -> None:
        path = agy_root / "cache" / "last_conversations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries), encoding="utf-8")
        os.utime(path, (at, at))

    out: dict = {}
    audit = role_now("audit")
    converse(audit, "agy", AUDIT_CONVERSATION, workspace=audit.workdir)  # Audit's agy conversation, from its agy days

    # 1. syrd's sequence: agy, Copilot through three set-asides, agy again; agy's map names Audit's conversation.
    reset("inspector", "agy")
    inspector = role_now("inspector")
    agy_map({inspector.workdir: AUDIT_CONVERSATION, audit.workdir: AUDIT_CONVERSATION}, at=time.time() - 3600)
    recorded(inspector, "agy", INSPECTOR_CONVERSATION)
    switch("inspector", "copilot")
    for n in (1, 2, 3):
        recorded(role_now("inspector"), "copilot", sid(n))
        switch("inspector", "copilot")  # a model change, or a resume refused: the record set aside on Copilot
    recorded(role_now("inspector"), "copilot", sid(4))
    switch("inspector", "agy")
    out["switched_back"] = {"kept": kept(role_now("inspector"))}
    out["return"] = start("inspector")
    out["return_again"] = start("inspector")

    # 2. A change of model on the same runtime keeps nothing: it starts fresh, as it always did.
    reset("inspector", "agy")
    recorded(role_now("inspector"), "agy", sid(10))
    switch("inspector", "agy")
    out["model_change"] = start("inspector")

    # 3. Back on agy, but the conversation it left there is now filed elsewhere: refused, set aside, not retried.
    reset("inspector", "agy")
    recorded(role_now("inspector"), "agy", sid(11))
    switch("inspector", "copilot")
    converse(role_now("inspector"), "agy", sid(11), workspace=audit.workdir)
    switch("inspector", "agy")
    out["refused"] = start("inspector")
    out["refused_again"] = start("inspector")

    # 4. Back on agy with a fresh start asked for: the kept conversation is dropped, and not taken later.
    reset("inspector", "agy")
    recorded(role_now("inspector"), "agy", sid(12))
    switch("inspector", "copilot")
    switch("inspector", "agy")
    out["fresh_asked"] = start("inspector", prefer_resume=False)
    out["fresh_asked_then"] = start("inspector")

    # 5. Back on agy where a record of its own is already there: that one resumes, and the kept one is dropped.
    reset("inspector", "agy")
    recorded(role_now("inspector"), "agy", sid(13))
    switch("inspector", "copilot")
    switch("inspector", "agy")
    recorded(role_now("inspector"), "agy", sid(14))
    out["own_record"] = start("inspector")

    # 6. The other way round: Claude, to agy, and back to Claude, resumes Claude's conversation.
    reset("audit", "claude")
    recorded(role_now("audit"), "claude", sid(15))
    switch("audit", "agy")
    recorded(role_now("audit"), "agy", sid(16))
    switch("audit", "claude")
    out["claude_return"] = start("audit")

    # 7. A kept record that is a link is not followed: dropped, and the start is fresh.
    reset("inspector", "copilot")
    elsewhere = work / "elsewhere.json"
    elsewhere.write_text(json.dumps({"target": role_now("inspector").target, "session_id": INSPECTOR_CONVERSATION,
                                     "source": "gemini.SessionStart"}), encoding="utf-8")
    runtimes["inspector"] = "agy"
    (own_dir(role_now("inspector")) / f"{name(role_now('inspector'))}.runtime-agy").symlink_to(elsewhere)
    out["link"] = start("inspector")
    out["link"]["target_left"] = elsewhere.is_file()

    # 8. attach-role brings a role back on the runtime it returned to with that runtime's conversation.
    reset("app", "agy")
    recorded(role_now("app"), "agy", sid(17))
    switch("app", "claude")
    switch("app", "agy")
    config, config_path = load()
    tmux = Tmux()
    with redirect_stderr(io.StringIO()):
        try:
            code = launcher.attach_role_to_slot(config, config_path=config_path, role_name="app", slot=4,
                                                session_dir=own_dir(role_now("app")), pane_state_dir=state,
                                                runner=tmux, print_func=lambda *_: None)
        except SystemExit as exc:
            code = f"exit: {exc}"
    out["attach"] = {"code": code, "resumed": [s for s in ids if s in " ".join(tmux.started())],
                     "events": [e["event"] for e in history(role_now("app"))][-3:], "kept": kept(role_now("app"))}

    # 8b. attach-role for a role fresh for each ticket, whose board says this launch is fresh: the kept conversation
    # is not left behind as the record, so the next launch -- told to resume what is recorded -- does not take it.
    from scripts import role_context_recovery

    raw_app = next(r for r in raw["roles"] if r["role"] == "app")
    raw_app["fresh_session_per_ticket"] = True
    reset("app", "agy")
    recorded(role_now("app"), "agy", sid(19))
    switch("app", "claude")
    switch("app", "agy")
    real_decide = role_context_recovery.decide
    role_context_recovery.decide = lambda role, recorded, runtime: role_context_recovery.Recovery(
        "fresh", event="fresh_per_ticket", message="team-launcher: the board says this launch is fresh")
    config, config_path = load()
    tmux = Tmux()
    with redirect_stderr(io.StringIO()):
        try:
            code = launcher.attach_role_to_slot(config, config_path=config_path, role_name="app", slot=4,
                                                session_dir=own_dir(role_now("app")), pane_state_dir=state,
                                                runner=tmux, print_func=lambda *_: None)
        except SystemExit as exc:
            code = f"exit: {exc}"
    out["attach_fresh"] = {"code": code, "resumed": [s for s in ids if s in " ".join(tmux.started())],
                           "events": [e["event"] for e in history(role_now("app"))][-4:], "kept": kept(role_now("app")),
                           "recorded": launcher.session_id_for_role(role_now("app"), own_dir(role_now("app")))}
    role_context_recovery.decide = lambda role, recorded, runtime: role_context_recovery.Recovery(
        "resume", session_id=recorded, event="recovery_resume_recorded", message="team-launcher: resume what is recorded")
    try:
        out["attach_fresh_next"] = start("app")
    finally:
        role_context_recovery.decide = real_decide
        raw_app.pop("fresh_session_per_ticket")

    # 9. As root for the owner, the switch keeps the record by descriptor, as it sets it aside.
    from scripts import owner_writes

    reset("inspector", "agy")
    recorded(role_now("inspector"), "agy", sid(18))
    runtimes["inspector"] = "copilot"
    real = (owner_writes.root_writes_for, owner_writes.owner_home, launcher.root_writes_for)
    owner_writes.root_writes_for = launcher.root_writes_for = lambda _user: True
    owner_writes.owner_home = lambda _user: str(home)
    try:
        config, _ = load()
        with redirect_stderr(io.StringIO()):
            try:
                role_runtime._restart_worker(config, role_name="inspector", pane_state_dir=state, runner=Tmux(),
                                             print_func=lambda *_: None)
                out["as_root"] = {"kept": kept(role_now("inspector"))}
            except SystemExit as exc:
                out["as_root"] = {"error": str(exc), "kept": kept(role_now("inspector"))}
    finally:
        owner_writes.root_writes_for, owner_writes.owner_home, launcher.root_writes_for = real
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd622.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), tmp],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    back = out["return"]
    check(back["resumed"] == [] and back["events"][-3:] == ["agy_map_not_taken", "no_record", "fresh_started"],
          f"before: back on agy, Inspector starts fresh, its own agy conversation forgotten: {back}")
    check(INSPECTOR_CONVERSATION not in [v[0] for v in out["switched_back"]["kept"].values()],
          f"before: after the round trip nothing in Inspector's store names its agy conversation: {out['switched_back']}")
    check(out["claude_return"]["resumed"] == [],
          f"before: so does a role back on Claude from agy: {out['claude_return']}")
    check(out["attach"]["resumed"] == [], f"before: and attach-role: {out['attach']}")


def candidate(out: dict) -> None:
    check(out["switched_back"]["kept"].get(".runtime-agy") == [INSPECTOR_CONVERSATION, "0o600"]
          and ".runtime-copilot" in out["switched_back"]["kept"],
          f"each switch keeps the conversation it leaves, private, as `.runtime-<runtime>`: {out['switched_back']}")
    back, again = out["return"], out["return_again"]
    check(back["code"] == 0 and back["resumed"] == [INSPECTOR_CONVERSATION]
          and back["events"] == ["adopted_return_record", "resumed"] and "agy_map_not_taken" not in back["events"]
          and ".runtime-agy" not in back["kept"] and back["kept"].get("") == [INSPECTOR_CONVERSATION, "0o600"],
          f"syrd's sequence: back on agy, Inspector resumes its own agy conversation, never Audit's the map names: {back}")
    check(set(back["verbs"]) <= {"has-session", "new-session", "set-option", "display-message", "list-panes"},
          f"nothing is typed into a pane: {back['verbs']}")
    check(again["resumed"] == [INSPECTOR_CONVERSATION] and again["events"] == ["resumed"],
          f"and resumes it again at the next start: {again}")
    model = out["model_change"]
    check(model["resumed"] == [] and "no_record" in model["events"] and not any(k.startswith(".runtime-") for k in model["kept"]),
          f"a change of model on the same runtime keeps nothing, and starts fresh as before: {model}")
    refused, refused_again = out["refused"], out["refused_again"]
    check(refused["resumed"] == [] and refused["events"][:2] == ["adopted_return_record", "foreign_workspace"]
          and refused["kept"].get(".foreign-workspace") == [sid(11), "0o600"] and ".runtime-agy" not in refused["kept"],
          f"a kept conversation SYRD-638 refuses is set aside with its reason, and the start is fresh: {refused}")
    check(refused_again["resumed"] == [] and "adopted_return_record" not in refused_again["events"],
          f"and it is not tried again: {refused_again}")
    fresh, then = out["fresh_asked"], out["fresh_asked_then"]
    check(fresh["resumed"] == [] and fresh["events"][0] == "return_record_dropped"
          and "a fresh session was asked for" in fresh["detail"][0] and ".runtime-agy" not in fresh["kept"]
          and then["resumed"] == [] and "adopted_return_record" not in then["events"],
          f"a fresh start asked for drops the kept conversation, and a later start does not take it: {fresh} {then}")
    own = out["own_record"]
    check(own["resumed"] == [sid(14)] and own["events"][0] == "return_record_dropped"
          and f"it has its own record {sid(14)}" in own["detail"][0] and ".runtime-agy" not in own["kept"],
          f"a record of its own on that runtime wins; the kept one is dropped: {own}")
    check(out["claude_return"]["resumed"] == [sid(15)] and out["claude_return"]["events"][0] == "adopted_return_record",
          f"Claude to agy and back resumes the Claude conversation: {out['claude_return']}")
    link = out["link"]
    check(link["resumed"] == [] and link["events"][0] == "return_record_dropped" and "it is not a record" in link["detail"][0]
          and ".runtime-agy" not in link["kept"] and link["target_left"],
          f"a kept record that is a link is dropped unread, its target left alone, and the start is fresh: {link}")
    check(out["attach"]["code"] == 0 and out["attach"]["resumed"] == [sid(17)] and "adopted_return_record" in out["attach"]["events"],
          f"attach-role resumes the conversation the role left that runtime with: {out['attach']}")
    fresh, nxt = out["attach_fresh"], out["attach_fresh_next"]
    check(fresh["code"] == 0 and fresh["resumed"] == [] and fresh["recorded"] == ""
          and ".runtime-agy" not in fresh["kept"] and fresh["kept"].get(".superseded", [None])[0] == sid(19),
          f"attach-role starting a fresh-per-ticket role fresh leaves no record naming the kept conversation: {fresh}")
    check(nxt["resumed"] == [] and sid(19) not in json.dumps(nxt["events"]) and "no_record" in nxt["events"],
          f"and the next launch, told to resume what is recorded, does not take the old conversation: {nxt}")
    check(out["as_root"].get("kept", {}).get(".runtime-agy") == [sid(18), "0o600"] and "error" not in out["as_root"]
          and out["as_root"]["kept"].get(".superseded", [None])[0] == sid(18),
          f"as root for the owner the record is kept by descriptor and set aside as before: {out['as_root']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD622_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd622-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"runtime_return_record_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
