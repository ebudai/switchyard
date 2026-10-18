#!/usr/bin/env python3
"""SYRD-569: a conversation the User resumes by hand in a role's pane is the one the next start resumes.

MEFP's Main came up fresh on 2026-10-13, on a release with the earlier SYRD-569
fix, and its only log was empty. Two things are pinned here.

**The trap.** After any fresh start, the pane's provider records its new,
unused conversation: Claude at once, at SessionStart; Codex at its first input,
which a listener nudge provides. When the User then types `/resume` to get the
real conversation back, the provider announces it with SessionStart (`resume`),
and the hook refused it as "foreign": the record had an id, and the pane was
launched with none to match. So the record kept the unused conversation, and
the next start found it empty (fresh again) or resumed the nudge instead. Every
start needed the User again. The hook now accepts a conversation the pane's own
provider announces with SessionStart. A CLI started inside the pane (a role's
`claude -p` or `codex exec`), which inherits the pane's environment, is still
refused, as is a hook run outside any pane.

**Durable diagnostics.** Every start's decision, and every change the hook
makes or refuses to the record, is kept in `<pane>.resume.log` beside the
record, with the record files the start could see.

The hooks are the real installed ones, run by a stand-in provider process in a
real tmux server on a private socket, in a user namespace with no network,
with the environment the launcher builds for the pane: the walk from the hook
to the tmux server is the real one. Measured on this host: Claude Code 2.1.294
and Codex 0.162.0 run a hook as their direct child, and the stand-in does the
same. The starts are the real `_start_role_session` and `attach_role_to_slot`,
with the real resume preflight over provider stores in a test home. tmux is
stood in for there, and any other command is refused. Claude, Codex and
Antigravity (agy) are covered. The before-run is main's code, from a git
archive.
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
BEFORE = "4a4ecb444ad314a7ddc387842769920ef1d03b6e"  # main before this change
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "PGU_PANE_", "DIRECTORCTL")
PROVIDERS = {"director": "claude", "ops": "codex", "audit": "agy"}
EVENTS = {"claude": ("claude", "SessionStart", "UserPromptSubmit"), "codex": ("codex", "SessionStart", "UserPromptSubmit"),
          "agy": ("gemini", "SessionStart", "PreInvocation")}
CHECKS = 0

PROVIDER = r'''
import json, os, subprocess, sys, time
steps_path, done = sys.argv[1], sys.argv[2]
steps = json.load(open(steps_path))

def run(step):
    subprocess.run(["sh", "-c", step["command"]], input=json.dumps(step["payload"]), text=True,
                   capture_output=True, timeout=60)

if len(sys.argv) > 3:  # a CLI the role started from its tool shell: it runs its own hooks
    run(steps[int(sys.argv[3])])
    raise SystemExit(0)
for i, step in enumerate(steps):
    if step["via"] == "transient":
        subprocess.run(["bash", "-c", f"{sys.executable} {sys.argv[0]} {steps_path} {done} {i}; true"], timeout=90)
    else:
        run(step)
open(done, "w").write("done")
'''


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000569"


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
        if role["role"] == "audit":
            role["cli"] = ["agy"]
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

    class Tmux:
        """tmux stood in for; any other command is refused."""

        def __init__(self, *, dies: bool = False) -> None:
            self.calls: list[list[str]] = []
            self.live: set[str] = set()
            self.dies = dies  # a resumed provider that exits at once

        def __call__(self, args, **_kwargs):
            args = [str(a) for a in args]
            self.calls.append(args)
            if args[:1] != ["tmux"]:
                raise AssertionError(f"refused: {args}")
            if args[1] == "has-session":
                return subprocess.CompletedProcess(args, 0 if args[3] in self.live else 1, "", "")
            if args[1] == "new-session":
                if not (self.dies and "resume" in args[-1] or self.dies and "--conversation" in args[-1]):
                    self.live.add(args[args.index("-s") + 1])
            if args[1] == "kill-session":
                self.live.discard(args[3])
            return subprocess.CompletedProcess(args, 0, "", "")

        def started(self) -> list[str]:
            return [call[-1] for call in self.calls if call[1:2] == ["new-session"]]

    live = {"value": True}
    launcher.live_command_matches_role = lambda role, runner=None: live["value"]
    launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.3
    launcher.RESUME_STARTUP_POLL_SECONDS = 0.05

    def transcript(role, session_id: str) -> Path:
        cli = PROVIDERS[role.role]
        if cli == "claude":
            return launcher._claude_project_dir_for_workdir(role.workdir, home=home) / f"{session_id}.jsonl"
        if cli == "codex":
            return home / ".codex" / "sessions" / "2026" / "10" / "13" / f"rollout-2026-10-13T07-26-39-{session_id}.jsonl"
        return home / ".gemini" / "antigravity-cli" / "conversations" / f"{session_id}.db"

    def converse(role, session_id: str) -> None:
        """The provider's store holds this conversation: something was said in it."""
        path = transcript(role, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    def own_dir(role) -> Path:
        return launcher.role_session_dir(config, role)

    def seed(role, session_id: str) -> str:
        """The role's record names a conversation its provider has."""
        converse(role, session_id)
        path = own_dir(role) / launcher.session_file_name(role.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"target": role.target, "session_id": session_id, "updated_at": time.time(),
                                    "source": "test", "payload": payload(role, session_id, "resume")}) + "\n",
                        encoding="utf-8")
        return recorded(role)

    def recorded(role) -> str:
        return launcher.session_id_for_role(role, own_dir(role))

    def history(role) -> list[dict]:
        safe = launcher.session_file_name(role.target).removesuffix(".json")
        path = own_dir(role) / f"{safe}.resume.log"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def start(role, *, tmux=None) -> dict:
        tmux = tmux or Tmux()
        err = io.StringIO()
        with redirect_stderr(err):
            code = launcher._start_role_session(role, session_dir=own_dir(role), pane_state_dir=state, prefer_resume=True,
                                                seed_source="syrd569.start", runner=tmux)
        first = tmux.started()[0] if tmux.started() else ""
        return {"code": code, "starts": len(tmux.started()), "resumed": next((s for s in all_ids if s in first), ""),
                "said": err.getvalue()}

    all_ids = [sid(n) for n in range(1, 60)]

    # The real installer, into the test home.
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

    def payload(role, session_id: str, source: str) -> dict:
        body = {"session_id": session_id, "hook_event_name": "SessionStart", "source": source,
                "cwd": role.workdir}
        if PROVIDERS[role.role] != "agy":  # Claude and Codex name their transcript; agy does not
            body["transcript_path"] = str(transcript(role, session_id))
        return body

    sock_dir = Path(tempfile.mkdtemp(prefix="s569.", dir="/tmp"))
    socket = sock_dir / "s"
    provider_script = work / "provider.py"
    provider_script.write_text(PROVIDER, encoding="utf-8")
    tmux_env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "SHELL": "/bin/sh", "TERM": "xterm-256color"}
    # The server outlives every pane: a pane whose command ends at once must not take it down under the client.
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

    def step(role, kind: str, session_id: str, source: str = "startup", via: str = "pane") -> dict:
        prefix, start_event, turn_event = EVENTS[PROVIDERS[role.role]]
        event = start_event if kind == "start" else turn_event
        body = payload(role, session_id, source)
        if kind != "start":
            body.update(hook_event_name=event, prompt="You have a new ticket.")
            body.pop("source")
        return {"via": via, "command": installed(PROVIDERS[role.role], event), "payload": body}

    out: dict = {}
    try:
        # 1. The trap, for each provider: a fresh start, its unused conversation recorded, the User's /resume.
        trap = {}
        for n, (name, cli) in enumerate(sorted(PROVIDERS.items()), start=1):
            role = roles[name]
            fresh, wanted = sid(10 + n), sid(20 + n)
            converse(role, wanted)  # the conversation the role had been in
            first = start(role)
            if cli == "codex":  # Codex says nothing until its first input: the listener's nudge
                steps = [step(role, "turn", fresh)]
                converse(role, fresh)
            else:
                steps = [step(role, "start", fresh)]
            steps.append(step(role, "start", wanted, source="resume"))  # the User's /resume, in the pane
            ran = in_pane(role, steps, resume=False, n=n)
            after_resume = recorded(role)
            second = start(role)
            trap[name] = {"first": first, "ran": ran, "after_resume": after_resume, "second": second,
                          "history": history(role), "fresh": fresh, "wanted": wanted}
        out["trap"] = trap

        # 2. A resumed pane whose provider opens another conversation (/clear): that one is next.
        role = roles["director"]
        cleared = sid(31)
        launched = seed(role, sid(30))
        resumed_env = pane_env(role, resume=True).get("TICKET_BOARD_PANE_SESSION_ID", "")
        ran = in_pane(role, [step(role, "start", cleared, source="clear")], resume=True, n=11)
        out["clear_in_resumed_pane"] = {"ran": ran, "launched": launched, "pane_env_id": resumed_env,
                                        "recorded": recorded(role)}

        # 3. What is still refused: a CLI started inside the pane, at its start and at its turns, and a turn
        #    event with another id from the pane's own provider -- except agy's, which is its switch (SYRD-622).
        refused = {}
        for n, (name, cli) in enumerate(sorted(PROVIDERS.items()), start=1):
            role = roles[name]
            before = seed(role, sid(35 + n))
            ran = in_pane(role, [step(role, "start", sid(40 + n), via="transient"),
                                 step(role, "turn", sid(40 + n), via="transient"),
                                 step(role, "turn", sid(45 + n))], resume=True, n=20 + n)
            refused[name] = {"ran": ran, "before": before, "after": recorded(role), "history": history(role)[-3:]}
        out["refused"] = refused
        # ... and a hook run outside any pane (a test, a hand-run hook) with the pane's environment.
        role = roles["director"]
        before = seed(role, sid(39))
        subprocess.run(["sh", "-c", installed("claude", "SessionStart")], input=json.dumps(payload(role, sid(50), "startup")),
                       env=pane_env(role, resume=True), capture_output=True, text=True, timeout=60)
        out["outside_pane"] = {"before": before, "after": recorded(role)}
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=30)
        shutil.rmtree(sock_dir, ignore_errors=True)

    # 4. Every start decision is kept, with the records it could see.
    decisions = {}
    role = roles["director"]
    shutil.rmtree(own_dir(role), ignore_errors=True)
    shared.mkdir(parents=True, exist_ok=True)
    stranded = shared / launcher.session_file_name(role.target)
    stranded.write_text(json.dumps({"target": role.target, "session_id": sid(51), "updated_at": time.time(),
                                    "source": "test", "payload": None}) + "\n", encoding="utf-8")
    decisions["stranded_path"] = str(stranded)
    decisions["adopted_missing"] = start(role)  # taken back from the shared store; the provider no longer has it
    converse(role, sid(52))
    (own_dir(role) / launcher.session_file_name(role.target)).write_text(json.dumps(
        {"target": role.target, "session_id": sid(52), "updated_at": time.time() + 5, "source": "test", "payload": None}),
        encoding="utf-8")
    decisions["resumed"] = start(role)
    live["value"] = False  # the resumed provider exits at once: fresh, after it
    decisions["resume_dies"] = start(role, tmux=Tmux(dies=True))
    (own_dir(role) / launcher.session_file_name(role.target)).write_text(json.dumps(
        {"target": role.target, "session_id": sid(52), "updated_at": time.time() + 10, "source": "test", "payload": None}),
        encoding="utf-8")
    decisions["unverified"] = start(role)  # the provider never shows, but its session stays: unverified
    live["value"] = True
    decisions["history"] = history(role)
    safe = launcher.session_file_name(role.target).removesuffix(".json")
    log = own_dir(role) / f"{safe}.resume.log"
    decisions["mode"] = stat.S_IMODE(log.stat().st_mode) if log.exists() else None
    # attach-role says why it could not attach, there too
    app = roles["app"]
    with redirect_stderr(io.StringIO()):
        code = launcher.attach_role_to_slot(load(), config_path=config_path, role_name="app", slot=4,
                                            session_dir=own_dir(app), pane_state_dir=state, runner=Tmux(),
                                            print_func=lambda *_: None)
    decisions["attach"] = {"code": code, "history": history(app)}
    out["decisions"] = decisions

    # 5. The history stays bounded, newest kept.
    try:
        from scripts import resume_history
    except ImportError:
        out["bounded"] = None
    else:
        before_size = log.stat().st_size
        filler = "x" * 2000
        for i in range(200):
            resume_history.record_resume_decision(role, own_dir(role), "filler", detail=f"{i} {filler}")
        entries = history(role)
        out["bounded"] = {"size": log.stat().st_size, "limit": resume_history.RESUME_HISTORY_MAX_BYTES,
                          "before_size": before_size, "last": entries[-1]["detail"][:4] if entries else "",
                          "count": len(entries), "path": str(resume_history.resume_history_path(role.target, own_dir(role))),
                          "log": str(log), "mode": stat.S_IMODE(log.stat().st_mode)}
    return out


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


# --- the parent -------------------------------------------------------------------------------------------------------


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run_child(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd569m.") as tmp:
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
    if os.environ.get("SYRD569_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd569m-before.") as tmp:
            before_run(run_child(tree_at(BEFORE, Path(tmp) / "before")))
    now = sorted((p.name, p.stat().st_mtime_ns) for p in real.rglob("*")) if real.is_dir() else []
    check(now == untouched, f"the real account's pgu session store is untouched: {sorted(set(now) ^ set(untouched))}")
    candidate(after)
    print(f"manual_resume_record_test: {CHECKS} checks ok")
    return 0


def before_run(before: dict) -> None:
    for name, got in before["trap"].items():
        check(got["ran"]["done"] and got["after_resume"] == got["fresh"] and got["second"]["resumed"] != got["wanted"],
              f"before: {name}'s ({PROVIDERS[name]}) record keeps the unused conversation after the User's /resume, "
              f"and the next start does not resume the one the User restored: {got}")
        check(not got["history"], f"before: {name}'s start leaves no history of what it decided: {got['history']}")
    check(before["clear_in_resumed_pane"]["launched"] == before["clear_in_resumed_pane"]["pane_env_id"] == sid(30)
          and before["clear_in_resumed_pane"]["recorded"] == sid(30),
          f"before: a /clear in a resumed pane is refused: {before['clear_in_resumed_pane']}")


def candidate(after: dict) -> None:
    for name, got in after["trap"].items():
        cli = PROVIDERS[name]
        check(got["ran"]["done"], f"{name}: the stand-in provider ran its hooks in the pane: {got['ran']}")
        check(got["first"]["code"] == 0 and got["first"]["resumed"] == "",
              f"{name}: the first start finds no record and starts fresh: {got['first']}")
        check(got["after_resume"] == got["wanted"],
              f"{name}'s {cli} hook records the conversation the User resumed in the pane: {got['after_resume']} vs {got}")
        check(got["second"]["code"] == 0 and got["second"]["starts"] == 1 and got["second"]["resumed"] == got["wanted"],
              f"and {name}'s next start resumes it, without the User: {got['second']}")
        events = [(e["by"], e["event"]) for e in got["history"]]
        check(events == [("launcher", "no_record"), ("launcher", "fresh_started"), ("hook", "recorded"),
                         ("hook", "recorded"), ("launcher", "resumed")],
              f"{name}'s history says each step, in order: {events}")
        recorded = [e for e in got["history"] if e["event"] == "recorded"]
        check(recorded[0]["session_id"] == got["fresh"] and recorded[0]["previous"] == ""
              and recorded[1]["session_id"] == got["wanted"] and recorded[1]["previous"] == got["fresh"]
              and isinstance(recorded[1]["provider_pid"], int) and recorded[1]["source"].endswith("SessionStart"),
              f"{name}: the hook's entries name the conversation it recorded, the one it replaced, and the provider: {recorded}")
        first_no_record = got["history"][0]
        check("no recorded session id" in first_no_record["detail"] and first_no_record["entry"] == "syrd569.start"
              and isinstance(first_no_record["records"], dict),
              f"{name}: the fresh start's entry carries the message it printed, the entry point and what it saw: {first_no_record}")

    got = after["clear_in_resumed_pane"]
    check(got["ran"]["done"] and got["launched"] == got["pane_env_id"] == sid(30) and got["recorded"] == sid(31),
          f"a resumed pane whose provider opens another conversation (/clear) records that one: {got}")

    for name, got in after["refused"].items():
        n = sorted(PROVIDERS).index(name) + 1
        kept = [e for e in got["history"] if e["by"] == "hook"]
        if PROVIDERS[name] == "agy":
            # SYRD-622: agy announces a /resume only through its next turn, so a turn from the pane's own agy
            # with another id is that switch; the CLI started inside the pane is still refused.
            check(got["ran"]["done"] and got["before"] == sid(35 + n) and got["after"] == sid(45 + n)
                  and [e["event"] for e in kept] == ["kept", "kept", "recorded"]
                  and all(e["session_id"] == got["before"] for e in kept[:2]) and kept[2]["previous"] == got["before"],
                  f"{name}: a CLI started inside the pane cannot replace the record, and the pane's own agy turn "
                  f"with another id does (tests/agy_resume_record_test.py): {got}")
            continue
        check(got["ran"]["done"] and got["after"] == got["before"] == sid(35 + n),
              f"{name}: a CLI started inside the pane, and a turn with another id, cannot replace the record: {got}")
        check(len(kept) == 3 and all(e["event"] == "kept" and e["session_id"] == got["before"] for e in kept),
              f"{name}: each refusal is kept in the history, naming the conversation kept and the one refused: {kept}")
    got = after["outside_pane"]
    check(got["after"] == got["before"] == sid(39), f"a hook run outside any pane cannot replace it either: {got}")

    d = after["decisions"]
    events = [e["event"] for e in d["history"]]
    check(events[:3] == ["adopted_shared", "preflight_refused", "fresh_started"],
          f"a taken-back conversation the provider lacks: adopted, refused by the preflight, fresh: {events}")
    refused = d["history"][1]
    check("is not present" in refused["detail"] and refused["session_id"] == sid(51)
          and refused["records"].get(d["stranded_path"], {}).get("session_id") == sid(51),
          f"the refusal keeps the preflight's reason and the records it saw, the shared one included: {refused}")
    check(d["resumed"]["resumed"] == sid(52) and "resumed" in events[3:4],
          f"a verified resume is kept: {events}, {d['resumed']}")
    check(events[4:6] == ["resume_failed", "fallback_started"] and d["resume_dies"]["starts"] == 2,
          f"a resume whose provider exits is kept with its fallback: {events}")
    check(events[6:7] == ["resume_unverified"] and d["unverified"]["code"] == 0,
          f"a resume that never shows is kept as unverified: {events}")
    check(d["mode"] == 0o600, f"the history is private to the account: {oct(d['mode'] or 0)}")
    attach = d["attach"]
    check(attach["code"] == 1 and [e["event"] for e in attach["history"]] == ["no_record"]
          and attach["history"][0]["entry"] == "team_launcher.attach_role",
          f"attach-role keeps why it could not attach: {attach}")

    b = after["bounded"]
    check(b["path"] == b["log"], f"the launcher and the hook write one file per pane: {b['path']} vs {b['log']}")
    check(b["size"] <= b["limit"] and b["last"] == "199 " and 0 < b["count"] < 200 + 8,
          f"the history stays under its limit and keeps the newest entries: {b}")
    check(b["mode"] == 0o600, f"and stays private after it is trimmed: {oct(b['mode'])}")


if __name__ == "__main__":
    raise SystemExit(main())
