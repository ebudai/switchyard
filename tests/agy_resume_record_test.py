#!/usr/bin/env python3
"""SYRD-622: a conversation the User resumes in an agy pane is the one the next start resumes.

Otto's designer (agy) came up fresh after a power cut on 2026-10-18, though the
User had `/resume`d its real conversation in the pane two days before. agy
opens a resumed conversation with no SessionStart: only PreInvocation,
PostInvocation and Stop follow, naming it. The hook took a switch only from
SessionStart (SYRD-569), so it refused all 292 of them as foreign, and the
record kept the throwaway conversation of the fresh start. After a later
restart there was no record at all, the User resumed again, and the designer
took no turn before the outage: nothing was recorded, though agy's own
`cache/last_conversations.json` named the conversation for the worktree.

Pinned here:

* **The hook.** A turn hook from the pane's own agy, naming another
  conversation, is the User's switch, and is recorded. A turn from an agy
  started inside the pane (a role's tool shell) and a hook run outside any
  pane are still refused, and so are Claude's and Codex's turns with another
  id (they announce a switch with SessionStart).
* **The start.** With no record, an agy role resumes the conversation agy
  maps to its worktree, through the normal preflight. Not when the map
  predates the record the launcher set aside, not when that record was
  another runtime's, not for a role that starts fresh for each ticket, not for
  another runtime's role, and not from a map that is linked, malformed,
  oversized or names no conversation id. attach-role takes it too.

The harness is SYRD-569's (tests/manual_resume_record_test.py): the real
installed hooks, run by a stand-in provider that is the pane's own process in
a real tmux server on a private socket, in a user namespace with no network;
the real `_start_role_session` and `attach_role_to_slot`, with the real resume
preflight over provider stores in a test home, tmux stood in for there. agy
1.4.2 is a single Go binary that runs a hook command through /bin/sh as its
child, with nothing between it and the tmux server, which is what the stand-in
does. The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from manual_resume_record_test import PANE_ENV_PREFIXES, PROVIDER, shlex_quote, tree_at  # noqa: E402

BEFORE = "bef825d727b657b07ad1e2cd4ad0b37423abb8ed"  # main before this change
CHECKS = 0
PROVIDERS = {"director": "claude", "ops": "codex", "audit": "agy", "app": "agy"}
TURN = {"claude": "UserPromptSubmit", "codex": "UserPromptSubmit", "agy": "PreInvocation"}


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000622"


# --- the scenario: runs in a child, on `root`'s code ------------------------------------------------------------------


def scenario(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    from contextlib import redirect_stderr
    from dataclasses import replace

    from scripts import role_command
    from scripts import team_launcher as launcher

    home = work / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    raw = json.loads((root / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
    raw["run_as_user"] = getpass.getuser()
    for role in raw["roles"]:
        role["workdir"] = str(work / "worktrees" / role["role"])  # each in its own worktree, as tenants are (SYRD-638)
        if PROVIDERS.get(role["role"]) == "agy":  # as an agy role is configured: it resumes with --conversation
            role.update(cli=["agy"], resume_mode="flag", resume_flag="--conversation")
            role.pop("model", None)
        if role["role"] == "app":  # one slot free, for attach-role
            role["detached"] = True
            role.pop("slot", None)
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

    class Tmux:
        """tmux stood in for; any other command is refused."""

        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.live: set[str] = set()

        def __call__(self, args, **_kwargs):
            args = [str(a) for a in args]
            self.calls.append(args)
            if args[:1] != ["tmux"]:
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

    def converse(role, session_id: str) -> None:
        """The provider's store holds this conversation, run in the role's worktree (SYRD-638: as the provider files it)."""
        from provider_store_fixture import agy_conversation, write_session

        cli = PROVIDERS[role.role]
        if cli == "claude":
            path = launcher._claude_project_dir_for_workdir(role.workdir, home=home) / f"{session_id}.jsonl"
        elif cli == "codex":
            path = home / ".codex" / "sessions" / "2026" / "10" / "18" / f"rollout-2026-10-18T09-03-05-{session_id}.jsonl"
        else:
            agy_conversation(agy_root, session_id, role.workdir)
            return
        write_session(path, cli, session_id, role.workdir)

    def own_dir(role) -> Path:
        return launcher.role_session_dir(config, role)

    def record_path(role) -> Path:
        return own_dir(role) / launcher.session_file_name(role.target)

    def seed(role, session_id: str, source: str = "test") -> str:
        converse(role, session_id)
        record_path(role).parent.mkdir(parents=True, exist_ok=True)
        record_path(role).write_text(json.dumps({"target": role.target, "session_id": session_id, "updated_at": time.time(),
                                                 "source": source, "payload": None}) + "\n", encoding="utf-8")
        return recorded(role)

    def set_aside(role, session_id: str, source: str) -> float:
        """The launcher set this record aside (a runtime change, a failed resume): `.superseded`, no record."""
        seed(role, session_id, source)
        launcher.clear_session_record_for_role(role, own_dir(role))
        return (own_dir(role) / f"{launcher.session_file_name(role.target)}.superseded").stat().st_ctime

    def reset(role) -> None:
        shutil.rmtree(own_dir(role), ignore_errors=True)
        own_dir(role).mkdir(parents=True)

    def agy_map(entries: dict, *, at: float | None = None, raw: bytes | None = None) -> Path:
        """agy's own map of each working directory to the conversation it last opened there."""
        path = agy_root / "cache" / "last_conversations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        elif path.is_symlink() or path.exists():
            path.unlink()
        path.write_bytes(raw if raw is not None else json.dumps(entries, indent=2).encode())
        if at is not None:
            os.utime(path, (at, at))
        return path

    def recorded(role) -> str:
        return launcher.session_id_for_role(role, own_dir(role))

    def history(role) -> list[dict]:
        safe = launcher.session_file_name(role.target).removesuffix(".json")
        path = own_dir(role) / f"{safe}.resume.log"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    all_ids = [sid(n) for n in range(1, 99)]

    def start(role, *, tmux=None) -> dict:
        tmux = tmux or Tmux()
        err = io.StringIO()
        with redirect_stderr(err):
            code = launcher._start_role_session(role, session_dir=own_dir(role), pane_state_dir=state, prefer_resume=True,
                                                seed_source="syrd622.start", runner=tmux)
        first = tmux.started()[0] if tmux.started() else ""
        return {"code": code, "starts": len(tmux.started()), "resumed": next((s for s in all_ids if s in first), ""),
                "conversation_flag": "--conversation" in first, "said": err.getvalue(),
                "events": [e["event"] for e in history(role)], "recorded": recorded(role)}

    installer = root / "scripts" / "ticket-board-install-pane-hooks"
    subprocess.run([sys.executable, str(installer), "install", "--home", str(home), "--hook-source",
                    str(root / "scripts" / "ticket-board-pane-idle-hook"), "--bin-path",
                    str(home / ".local" / "bin" / "ticket-board-pane-idle-hook")],
                   env={"PATH": "/usr/bin:/bin", "HOME": str(home), "TICKET_BOARD_PROJECT": "pgu"},
                   check=True, capture_output=True, timeout=60)

    def installed(cli: str, event: str) -> str:
        if cli == "agy":
            named = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())["ticket-board-pane-state"]
            return named[event][0]["command"]
        path = home / (".claude/settings.json" if cli == "claude" else ".codex/hooks.json")
        hooks = json.loads(path.read_text())["hooks"][event]
        return next(h["command"] for e in hooks for h in e["hooks"] if "pane-idle-hook" in h["command"])

    def pane_env(role, *, resume: bool) -> dict:
        argv = role_command.cli_command_for_role(role, session_dir=own_dir(role), pane_state_dir=state, resume=resume)
        assert argv[0] == "env", argv[:3]
        values, i = {}, 1
        while i < len(argv):
            if argv[i] == "-u":
                i += 2
                continue
            key, sep, value = argv[i].partition("=")
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                break
            values[key] = value
            i += 1
        values.update(HOME=str(home), PATH="/usr/bin:/bin", TICKET_BOARD_URL="http://127.0.0.1:9")
        values.pop("TICKET_BOARD_SOCKET", None)
        return values

    def step(role, event: str, session_id: str, via: str = "pane") -> dict:
        body = {"session_id": session_id, "hook_event_name": event, "cwd": role.workdir}
        if event == "SessionStart":
            body["source"] = "startup"
        else:
            body["prompt"] = "You have a new ticket."
        return {"via": via, "command": installed(PROVIDERS[role.role], event), "payload": body}

    sock_dir = Path(tempfile.mkdtemp(prefix="s622.", dir="/tmp"))
    socket = sock_dir / "s"
    provider_script = work / "provider.py"
    provider_script.write_text(PROVIDER, encoding="utf-8")
    tmux_env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "SHELL": "/bin/sh", "TERM": "xterm-256color"}
    subprocess.run(["tmux", "-S", str(socket), "new-session", "-d", "-s", "keep", "sleep 900"], env=tmux_env,
                   check=True, capture_output=True, timeout=30)

    def in_pane(role, steps: list[dict], *, resume: bool, n: int) -> dict:
        """Run `steps` from a stand-in provider that is the pane's own process, in a real tmux pane."""
        steps_path, done = work / f"steps{n}.json", work / f"done{n}"
        steps_path.write_text(json.dumps(steps), encoding="utf-8")
        environ = pane_env(role, resume=resume)
        errors = work / f"provider{n}.err"
        command = " ".join(["env", "-i", *(f"{k}={shlex_quote(v)}" for k, v in environ.items()),
                            sys.executable, str(provider_script), str(steps_path), str(done), f"2>{errors}"])
        result = subprocess.run(["tmux", "-S", str(socket), "new-session", "-d", "-s", f"p{n}", command],
                                env=tmux_env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        deadline = time.monotonic() + 90
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        return {"done": done.exists(), "errors": errors.read_text()[-600:] if errors.exists() else ""}

    out: dict = {}
    audit = roles["audit"]
    try:
        # 1. Otto's designer: a fresh start records its throwaway conversation at SessionStart; the User's /resume
        #    is heard first from the next turn's PreInvocation, then PostInvocation and Stop, all naming it.
        fresh, wanted = sid(1), sid(2)
        converse(audit, wanted)
        first = start(audit)
        converse(audit, fresh)
        ran = in_pane(audit, [step(audit, "SessionStart", fresh), step(audit, "PreInvocation", wanted),
                              step(audit, "PostInvocation", wanted), step(audit, "Stop", wanted)], resume=False, n=1)
        after_resume = recorded(audit)
        hook_entries = [e for e in history(audit) if e["by"] == "hook"]
        second = start(audit)
        out["trap"] = {"first": first, "ran": ran, "after_resume": after_resume, "hook": hook_entries, "second": second,
                       "fresh": fresh, "wanted": wanted}

        # 2. Still refused: an agy the role started from its tool shell, at its turns; Claude's and Codex's own
        #    turns with another id; an agy hook run outside any pane.
        refused = {}
        for n, name in enumerate(("audit", "director", "ops"), start=10):
            role = roles[name]
            reset(role)
            before = seed(role, sid(n), source="test")
            if name == "audit":
                steps = [step(role, "PreInvocation", sid(n + 10), via="transient"),
                         step(role, "Stop", sid(n + 10), via="transient")]
            else:
                steps = [step(role, TURN[PROVIDERS[name]], sid(n + 10))]
            ran = in_pane(role, steps, resume=True, n=n)
            refused[name] = {"ran": ran, "before": before, "after": recorded(role),
                             "kept": [e for e in history(role) if e["by"] == "hook"]}
        role = audit
        reset(role)
        before = seed(role, sid(20))
        subprocess.run(["sh", "-c", installed("agy", "PreInvocation")],
                       input=json.dumps(step(role, "PreInvocation", sid(21))["payload"]),
                       env=pane_env(role, resume=True), capture_output=True, text=True, timeout=60)
        refused["outside_pane"] = {"before": before, "after": recorded(role)}
        out["refused"] = refused
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=30)
        shutil.rmtree(sock_dir, ignore_errors=True)

    # 3. The outage: no record, agy's map names the conversation for the worktree.
    starts: dict = {}
    now = time.time()
    reset(audit)
    converse(audit, sid(30))
    agy_map({audit.workdir: sid(30), "/elsewhere": sid(31)}, at=now - 5)
    starts["no_record"] = start(audit)
    starts["no_record"]["history"] = history(audit)[:2]

    # ... after the launcher set an agy record aside (the Director's model change), and the User resumed since.
    reset(audit)
    aside = set_aside(audit, sid(32), "gemini.SessionStart")
    converse(audit, sid(33))
    agy_map({audit.workdir: sid(33)}, at=aside + 60)
    starts["after_set_aside"] = start(audit)

    # ... but not from a map written before that set-aside: the launcher left the role fresh on purpose.
    reset(audit)
    converse(audit, sid(34))
    agy_map({audit.workdir: sid(34)}, at=now - 600)
    set_aside(audit, sid(35), "gemini.SessionStart")
    starts["map_predates"] = start(audit)
    starts["map_predates"]["history"] = history(audit)[:1]

    # ... nor when the role came to agy from another runtime: an older agy conversation is not its last.
    reset(audit)
    aside = set_aside(audit, sid(36), "claude.SessionStart")
    converse(audit, sid(37))
    agy_map({audit.workdir: sid(37)}, at=aside + 60)
    starts["from_claude"] = start(audit)
    starts["from_claude"]["history"] = history(audit)[:1]

    # ... a conversation agy no longer has: taken, refused by the preflight, fresh; and not taken again.
    reset(audit)
    agy_map({audit.workdir: sid(38)}, at=now - 5)
    starts["missing"] = start(audit)
    starts["missing_again"] = start(audit)

    # ... a map that is not agy's plain file, or does not name a conversation.
    target_map = agy_root / "cache" / "real.json"
    target_map.write_text(json.dumps({audit.workdir: sid(39)}), encoding="utf-8")
    converse(audit, sid(39))
    bad = {
        "linked": None, "a_directory": None, "not_json": b"{not json", "a_list": json.dumps([sid(39)]).encode(),
        "not_an_id": json.dumps({audit.workdir: "../../../etc/passwd"}).encode(),
        "oversized": json.dumps({audit.workdir: sid(39), "pad": "x" * (1 << 20)}).encode(),
        "no_entry": json.dumps({"/elsewhere": sid(39)}).encode(),
    }
    for name, raw in bad.items():
        reset(audit)
        if raw is None:
            path = agy_map({}, raw=b"{}")
            path.unlink()
            if name == "linked":
                path.symlink_to(target_map)
            else:
                path.mkdir()
        else:
            agy_map({}, raw=raw)
        starts[f"bad_{name}"] = start(audit)
        starts[f"bad_{name}"]["history"] = history(audit)[:1]

    # ... nor over a record the role has: the map is asked only when there is none.
    reset(audit)
    seed(audit, sid(42), "gemini.PreInvocation")
    converse(audit, sid(43))
    agy_map({audit.workdir: sid(43)}, at=time.time() + 60)
    starts["has_record"] = start(audit)

    # ... not for another runtime's role, nor for an agy role that starts fresh for each ticket.
    director = roles["director"]
    reset(director)
    converse(audit, sid(40))
    agy_map({director.workdir: sid(40), audit.workdir: sid(40)}, at=now - 5)
    starts["claude_role"] = start(director)
    per_ticket = replace(audit, fresh_session_per_ticket=True)
    reset(per_ticket)
    starts["fresh_per_ticket"] = start(per_ticket)
    out["starts"] = starts

    # 4. attach-role brings up a detached agy role from agy's map too.
    app = roles["app"]
    reset(app)
    converse(app, sid(41))
    agy_map({app.workdir: sid(41)}, at=now - 5)
    tmux = Tmux()
    with redirect_stderr(io.StringIO()):
        try:
            code = launcher.attach_role_to_slot(load(), config_path=config_path, role_name="app", slot=4,
                                                session_dir=own_dir(app), pane_state_dir=state, runner=tmux,
                                                print_func=lambda *_: None)
        except SystemExit as exc:
            code = f"exit: {exc}"
    out["attach"] = {"code": code, "events": [e["event"] for e in history(app)], "recorded": recorded(app),
                     "resumed": any(sid(41) in call for call in tmux.started())}
    return out


# --- the parent -------------------------------------------------------------------------------------------------------


def run_child(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd622.") as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES) and k not in ("TMUX", "TMUX_PANE")}
        env.update(HOME=str(work / "home"), PYTHONDONTWRITEBYTECODE="1")
        child = subprocess.run(["unshare", "--map-current-user", "--net", sys.executable, str(Path(__file__).resolve()),
                                "--scenario", str(root), str(work)], capture_output=True, text=True, env=env, timeout=900)
        assert child.returncode == 0, child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    real = Path.home() / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
    untouched = sorted((p.name, p.stat().st_mtime_ns) for p in real.rglob("*")) if real.is_dir() else []
    after = run_child(ROOT)
    if os.environ.get("SYRD622_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd622-before.") as tmp:
            before_run(run_child(tree_at(BEFORE, Path(tmp) / "before")))
    now = sorted((p.name, p.stat().st_mtime_ns) for p in real.rglob("*")) if real.is_dir() else []
    check(now == untouched, f"the real account's pgu session store is untouched: {sorted(set(now) ^ set(untouched))}")
    candidate(after)
    print(f"agy_resume_record_test: {CHECKS} checks ok")
    return 0


def before_run(before: dict) -> None:
    trap = before["trap"]
    check(trap["ran"]["done"] and trap["after_resume"] == trap["fresh"] and trap["second"]["resumed"] == trap["fresh"],
          f"before: agy's record keeps the throwaway conversation after the User's /resume, and the next start "
          f"resumes that one: {trap}")
    check(sum(e["event"] == "kept" for e in trap["hook"]) == 3,
          f"before: each of the resumed conversation's turn hooks is refused as foreign: {trap['hook']}")
    s = before["starts"]
    check(s["no_record"]["resumed"] == "" and s["after_set_aside"]["resumed"] == "",
          f"before: with no record, the start is fresh though agy names the worktree's conversation: {s['no_record']}, "
          f"{s['after_set_aside']}")


def candidate(after: dict) -> None:
    trap = after["trap"]
    check(trap["ran"]["done"], f"the stand-in agy ran its hooks in the pane: {trap['ran']}")
    check(trap["first"]["code"] == 0 and trap["first"]["resumed"] == "",
          f"the first start finds no record and no map, and starts fresh: {trap['first']}")
    check(trap["after_resume"] == trap["wanted"],
          f"agy's first turn after the User's /resume records the resumed conversation: {trap['after_resume']} vs {trap}")
    recorded = [e for e in trap["hook"] if e["event"] == "recorded"]
    check(len(recorded) == 2 and recorded[0]["session_id"] == trap["fresh"] and recorded[0]["source"] == "gemini.SessionStart"
          and recorded[1]["session_id"] == trap["wanted"] and recorded[1]["previous"] == trap["fresh"]
          and recorded[1]["source"] == "gemini.PreInvocation" and isinstance(recorded[1]["provider_pid"], int),
          f"the history names the switch, the turn that said it and the pane's provider: {trap['hook']}")
    check(not [e for e in trap["hook"] if e["event"] == "kept"],
          f"and nothing of the resumed conversation is refused: {trap['hook']}")
    check(trap["second"]["code"] == 0 and trap["second"]["starts"] == 1 and trap["second"]["resumed"] == trap["wanted"]
          and trap["second"]["conversation_flag"],
          f"the next start resumes it with agy --conversation, without the User: {trap['second']}")

    for name, got in after["refused"].items():
        if name == "outside_pane":
            check(got["after"] == got["before"] == sid(20), f"an agy hook run outside any pane cannot replace it: {got}")
            continue
        check(got["ran"]["done"] and got["after"] == got["before"],
              f"{name} ({PROVIDERS[name]}): {'an agy started from the tool shell' if name == 'audit' else 'a turn with another id'} "
              f"cannot replace the record: {got}")
        check(got["kept"] and all(e["event"] == "kept" and e["session_id"] == got["before"] for e in got["kept"]),
              f"{name}: each refusal is kept in the history: {got['kept']}")

    s = after["starts"]
    got = s["no_record"]
    check(got["code"] == 0 and got["resumed"] == sid(30) and got["conversation_flag"] and got["starts"] == 1
          and got["events"][:2] == ["adopted_agy_map", "resumed"],
          f"with no record, the start resumes the conversation agy maps to the worktree, through the preflight: {got}")
    taken = got["history"][0]
    check(taken["session_id"] == sid(30) and "last_conversations.json" in taken["detail"] and "SYRD-622" in taken["detail"],
          f"and keeps where it found it: {taken}")
    got = s["after_set_aside"]
    check(got["resumed"] == sid(33) and got["events"][:2] == ["adopted_agy_map", "resumed"],
          f"after an agy record was set aside, a conversation agy opened since is resumed: {got}")
    got = s["map_predates"]
    check(got["resumed"] == "" and got["starts"] == 1 and got["events"][:3] == ["agy_map_not_taken", "no_record", "fresh_started"]
          and "before" in got["history"][0]["detail"],
          f"a map written before the launcher set the record aside is not taken: {got}")
    got = s["from_claude"]
    check(got["resumed"] == "" and got["events"][:3] == ["agy_map_not_taken", "no_record", "fresh_started"]
          and "claude.SessionStart" in got["history"][0]["detail"],
          f"a role that came to agy from Claude does not inherit an older agy conversation: {got}")
    got, again = s["missing"], s["missing_again"]
    check(got["resumed"] == "" and got["events"][:3] == ["adopted_agy_map", "preflight_refused", "fresh_started"],
          f"a mapped conversation agy no longer has: taken, refused by the preflight, fresh: {got}")
    check(again["resumed"] == "" and again["events"][3:6] == ["agy_map_not_taken", "no_record", "fresh_started"],
          f"and not taken again at the next start: {again['events']}")
    for name, reason in (("linked", "not read"), ("a_directory", "not a regular file"), ("not_json", "not JSON"), ("a_list", "not a map"),
                         ("not_an_id", "not a conversation id"), ("oversized", "larger than"), ("no_entry", None)):
        got = s[f"bad_{name}"]
        check(got["code"] == 0 and got["resumed"] == "" and got["recorded"] == ""
              and got["events"][-2:] == ["no_record", "fresh_started"],
              f"a map that is {name.replace('_', ' ')} gives nothing to resume: {got}")
        if reason:
            check(got["events"][0] == "agy_map_not_taken" and reason in got["history"][0]["detail"],
                  f"and the start says why it was not taken ({name}): {got['history']}")
        else:
            check(got["events"] == ["no_record", "fresh_started"], f"a map without the worktree is silent: {got}")
    got = s["has_record"]
    check(got["resumed"] == sid(42) and got["events"][:1] == ["resumed"] and got["recorded"] == sid(42),
          f"a role with a record resumes it, whatever agy's map says: {got}")
    got = s["claude_role"]
    check(got["resumed"] == "" and "adopted_agy_map" not in got["events"],
          f"a Claude role is not given agy's conversation for its worktree: {got}")
    got = s["fresh_per_ticket"]
    check(got["resumed"] == "" and "adopted_agy_map" not in got["events"] and not got["recorded"],
          f"an agy role that starts fresh for each ticket takes nothing from the map: {got}")

    got = after["attach"]
    check(got["code"] == 0 and got["events"][:1] == ["adopted_agy_map"] and got["recorded"] == sid(41) and got["resumed"],
          f"attach-role brings a detached agy role up on agy's conversation for its worktree: {got}")


if __name__ == "__main__":
    raise SystemExit(main())
