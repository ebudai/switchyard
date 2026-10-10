#!/usr/bin/env python3
"""SYRD-569: a restarted role resumes the conversation its pane last recorded, without anyone typing /resume.

With `role_state_isolation`, the launcher reads a role's resumable session from
`<session_dir>/roles/<role>/`, and gives the pane that directory as
`TICKET_BOARD_PANE_SESSION_DIR`. The provider hooks are installed once per
account with `--session-dir <session_dir>`, and the hook let that flag win. So
every role recorded into the shared parent, and every restart found no record
(Otto: every role fresh after its upgrade) or a stale copy (SYRD: Main and
Audit fresh after the usage-reset restart, until the User resumed them).

The installer and the hook binary are the real ones, installed into a test home
and run exactly as the provider runs them (the installed command, with the
environment the launcher gives the pane and the provider's SessionStart input
on stdin, with no network). The launch is the real `_start_role_session` and
`attach_role_to_slot` with the real resume preflight, over provider stores in
the test home; tmux is stood in for, and any other process is refused. Claude,
Codex and Antigravity (agy) are covered. The before-run is main's code, in a
child built from a git archive.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "a025c35befb42bbff0b2b17e3f67888f46996daa"  # main before SYRD-569
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "PGU_PANE_", "DIRECTORCTL")
PROVIDERS = {"director": "claude", "ops": "codex", "audit": "agy"}
OLD, NEW = 1_790_000_000.0, 1_791_000_000.0
CHECKS = 0


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

    from scripts import team_launcher as launcher
    from scripts import role_command

    home = work / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    me = getpass.getuser()
    raw = json.loads((root / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
    raw["run_as_user"] = me
    for role in raw["roles"]:
        if role["role"] == "audit":  # SYRD's Audit runs agy; Main and the Director run Claude, ops Codex
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

    def load(isolated: bool = True):
        return replace(launcher.load_project_config("pgu", config_path), session_dir=shared,
                       role_state_isolation=isolated, desktop_access={"mode": "headless"})

    config = load()
    roles = {role.role: role for role in config.roles}

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
            if args[1] == "kill-session":
                self.live.discard(args[3])
            return subprocess.CompletedProcess(args, 0, "", "")

        def started(self) -> list[str]:
            return [call[-1] for call in self.calls if call[1:2] == ["new-session"]]

    launcher.live_command_matches_role = lambda role, runner=None: True  # the provider came up

    def provider_store(role, session_id: str) -> None:
        cli = launcher._command_name(role.cli[0])
        if cli == "claude":
            path = launcher._claude_project_dir_for_workdir(role.workdir, home=home) / f"{session_id}.jsonl"
        elif cli == "codex":
            path = home / ".codex" / "sessions" / "2026" / "10" / "10" / f"rollout-x-{session_id}.jsonl"
        else:
            path = home / ".gemini" / "antigravity-cli" / "conversations" / f"{session_id}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    def record(directory: Path, role, session_id: str, at: float, suffix: str = "", target: str | None = None) -> Path:
        path = directory / f"{launcher.session_file_name(role.target)}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"target": target or role.target, "session_id": session_id, "updated_at": at,
                                    "source": "test", "payload": None}) + "\n", encoding="utf-8")
        return path

    def files(directory: Path, role) -> dict:
        name = launcher.session_file_name(role.target)
        out = {}
        for suffix in ("", ".superseded", ".superseded.1"):
            path = directory / f"{name}{suffix}"
            if path.is_file():
                parsed = json.loads(path.read_text(encoding="utf-8"))
                out[suffix or "record"] = [parsed.get("session_id"), parsed.get("updated_at")]
        return out

    def start(role, isolated: bool = True) -> dict:
        tmux = Tmux()
        err = io.StringIO()
        session_dir = launcher.role_session_dir(load(isolated), role)
        with redirect_stderr(err):
            code = launcher._start_role_session(role, session_dir=session_dir, pane_state_dir=state, prefer_resume=True,
                                                seed_source="syrd569", runner=tmux)
        first = tmux.started()[0] if tmux.started() else ""
        resumed = next((s for s in all_ids if s in first), "")
        return {"code": code, "starts": len(tmux.started()), "resumed": resumed,
                "resume_args": bool(resumed) and shlex.join(role_command._resume_args_for_role(role, resumed)) in first,
                "own": files(session_dir, role), "shared": files(shared, role),
                "adopted_line": "SYRD-569" in err.getvalue(), "fresh_line": "starting fresh" in err.getvalue()}

    def reset() -> None:
        for path in (shared, home / ".claude", home / ".codex", home / ".gemini", state):
            shutil.rmtree(path, ignore_errors=True)

    all_ids = [sid(n) for n in range(1, 40)]
    out: dict = {}

    # 1. The hook, as each provider runs it at SessionStart, in a pane the launcher started.
    hook_home = home
    installer = root / "scripts" / "ticket-board-install-pane-hooks"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(hook_home), "TICKET_BOARD_PROJECT": "pgu",
           "TICKET_BOARD_PANE_STATE_DIR": str(state), "TICKET_BOARD_PANE_SESSION_DIR": str(shared)}
    subprocess.run([sys.executable, str(installer), "install", "--home", str(hook_home), "--hook-source",
                    str(root / "scripts" / "ticket-board-pane-idle-hook"), "--bin-path",
                    str(hook_home / ".local" / "bin" / "ticket-board-pane-idle-hook")],
                   env=env, check=True, capture_output=True, timeout=60)

    def installed_session_start(cli: str) -> str:
        if cli == "claude":
            hooks = json.loads((hook_home / ".claude" / "settings.json").read_text())["hooks"]["SessionStart"]
            return next(h["command"] for e in hooks for h in e["hooks"] if "pane-idle-hook" in h["command"])
        if cli == "codex":
            hooks = json.loads((hook_home / ".codex" / "hooks.json").read_text())["hooks"]["SessionStart"]
            return next(h["command"] for e in hooks for h in e["hooks"] if "pane-idle-hook" in h["command"])
        named = json.loads((hook_home / ".gemini" / "config" / "hooks.json").read_text())["ticket-board-pane-state"]
        return named["SessionStart"][0]["command"]

    def pane_env(role, session_dir: Path) -> dict:
        argv = role_command.cli_command_for_role(role, session_dir=session_dir, pane_state_dir=state)
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
        values.update(HOME=str(hook_home), PATH="/usr/bin:/bin", TICKET_BOARD_URL="http://127.0.0.1:9")
        values.pop("TICKET_BOARD_SOCKET", None)
        return values

    def run_hook(command: str, environ: dict, payload: dict) -> int:
        return subprocess.run(["unshare", "-rn", "sh", "-c", command], input=json.dumps(payload), text=True,
                              env=environ, capture_output=True, timeout=60).returncode

    hook = {}
    for n, (name, cli) in enumerate(sorted(PROVIDERS.items()), start=1):
        role = roles[name]
        own = launcher.role_session_dir(config, role)
        rc = run_hook(installed_session_start(cli), pane_env(role, own),
                      {"session_id": sid(n), "source": "startup", "hook_event_name": "SessionStart"})
        provider_store(role, sid(n))
        hook[name] = {"rc": rc, "launcher_reads": launcher.session_id_for_role(role, own),
                      "shared": launcher.session_id_for_role(role, shared), "restart": start(role)}
    out["hook"] = hook
    # Outside the pane's own target (a test, a hand-run hook), the flag still decides.
    elsewhere = work / "elsewhere"
    role = roles["director"]
    rc = run_hook(installed_session_start("claude").replace(str(shared), str(elsewhere)) + " --target pgu-other:0.0",
                  pane_env(role, launcher.role_session_dir(config, role)),
                  {"session_id": sid(30), "source": "startup", "hook_event_name": "SessionStart"})
    out["hook_other_target"] = {"rc": rc, "flag_dir": sorted(p.name for p in elsewhere.glob("*.json")),
                                "own_dir": sorted(p.name for p in launcher.role_session_dir(config, role).glob("pgu-other*"))}
    no_pane = {k: v for k, v in pane_env(role, launcher.role_session_dir(config, role)).items()
               if k not in ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")}
    rc = run_hook(installed_session_start("claude").replace(str(shared), str(elsewhere)), no_pane,
                  {"session_id": sid(31), "source": "startup", "hook_event_name": "SessionStart"})
    out["hook_no_pane_dir"] = {"rc": rc, "flag_dir": launcher.session_id_for_role(role, elsewhere)}
    # A legacy pgu pane names its directory only under the PGU_ name.
    (launcher.role_session_dir(config, role) / launcher.session_file_name(role.target)).unlink(missing_ok=True)
    legacy = {k: v for k, v in pane_env(role, launcher.role_session_dir(config, role)).items()
              if k != "TICKET_BOARD_PANE_SESSION_DIR"}
    rc = run_hook(installed_session_start("claude"), legacy,
                  {"session_id": sid(32), "source": "startup", "hook_event_name": "SessionStart"})
    out["hook_legacy_pane_dir"] = {"rc": rc, "own": launcher.session_id_for_role(role, launcher.role_session_dir(config, role)),
                                   "shared": launcher.session_id_for_role(role, shared)}
    reset()

    # 2. Records already stranded in the shared store, at the next restart.
    def case(label: str, setup, role_name: str = "director", isolated: bool = True, store: list[str] = ()) -> None:
        role = roles[role_name]
        own = shared / "roles" / role.role
        setup(role, own)
        for session_id in store:
            provider_store(role, session_id)
        shared_bytes = (shared / launcher.session_file_name(role.target)).read_bytes() \
            if (shared / launcher.session_file_name(role.target)).exists() else b""
        result = start(role, isolated)
        result["shared_untouched"] = shared_bytes == ((shared / launcher.session_file_name(role.target)).read_bytes()
                                                      if (shared / launcher.session_file_name(role.target)).exists() else b"")
        out[label] = result
        reset()

    case("otto_only_shared", lambda r, own: record(shared, r, sid(1), NEW), store=[sid(1)])
    case("otto_only_shared_agy", lambda r, own: record(shared, r, sid(2), NEW), role_name="audit", store=[sid(2)])
    case("otto_only_shared_codex", lambda r, own: record(shared, r, sid(3), NEW), role_name="ops", store=[sid(3)])
    case("same_id_older_copy", lambda r, own: (record(shared, r, sid(4), NEW), record(own, r, sid(4), OLD)), store=[sid(4)])
    case("older_other_copy", lambda r, own: (record(shared, r, sid(5), NEW), record(own, r, sid(6), OLD)), store=[sid(5), sid(6)])
    case("only_set_aside_copy", lambda r, own: (record(shared, r, sid(7), NEW), record(own, r, sid(7), OLD, ".superseded")),
         store=[sid(7)])
    case("own_newer", lambda r, own: (record(shared, r, sid(8), OLD), record(own, r, sid(9), NEW)), store=[sid(8), sid(9)])
    case("set_aside_since", lambda r, own: (record(shared, r, sid(10), OLD), record(own, r, sid(11), NEW, ".superseded")),
         store=[sid(10), sid(11)])
    case("other_target", lambda r, own: record(shared, r, sid(12), NEW, target="pgu-other:0.0"), store=[sid(12)])
    case("not_isolated", lambda r, own: (record(shared, r, sid(13), NEW), record(own, r, sid(14), OLD)), isolated=False,
         store=[sid(13), sid(14)])
    case("empty_shared_id", lambda r, own: (record(shared, r, "", NEW), record(own, r, sid(17), OLD)), store=[sid(17)])
    case("no_updated_at_older_file", lambda r, own: (record(shared, r, sid(15), NEW), record(own, r, sid(16), OLD),
                                                     _drop_time(shared / launcher.session_file_name(r.target), NEW)),
         store=[sid(15), sid(16)])

    # 3. A taken-back conversation the provider no longer has: fresh, and not taken back at the next restart either.
    role = roles["director"]
    record(shared, role, sid(20), NEW)
    first = start(role)
    second = start(role)
    out["missing_conversation"] = [first, second]
    reset()

    # 4. Attaching a detached role resumes its stranded conversation too.
    role = roles["app"]
    record(shared, role, sid(21), NEW)
    provider_store(role, sid(21))
    tmux = Tmux()
    err = io.StringIO()
    with redirect_stderr(err):
        code = launcher.attach_role_to_slot(load(), config_path=config_path, role_name="app", slot=4,
                                            session_dir=launcher.role_session_dir(config, role), pane_state_dir=state,
                                            runner=tmux, print_func=lambda *_: None)
    started = tmux.started()
    out["attach_role"] = {"code": code, "resumed": bool(started) and sid(21) in started[0], "said": err.getvalue()[-400:]}
    reset()

    # ... but not while it is running: its live provider owns the record.
    config_path.write_text(json.dumps(raw), encoding="utf-8")  # detached again
    detached = launcher._role_by_name(load(), "app").detached
    record(shared, role, sid(23), NEW)
    record(shared / "roles" / "app", role, sid(24), OLD)
    tmux = Tmux()
    tmux.live.add(role.tmux_session)
    with redirect_stderr(io.StringIO()):
        code = launcher.attach_role_to_slot(load(), config_path=config_path, role_name="app", slot=4,
                                            session_dir=launcher.role_session_dir(config, role), pane_state_dir=state,
                                            runner=tmux, print_func=lambda *_: None)
    out["attach_running"] = {"detached_before": detached, "code": code, "starts": len(tmux.started()),
                             "own": files(launcher.role_session_dir(config, role), role)}
    reset()

    # 5. A role already running is left alone: no start, nothing taken back.
    role = roles["director"]
    record(shared, role, sid(22), NEW)
    tmux = Tmux()
    tmux.live.add(role.tmux_session)
    with redirect_stderr(io.StringIO()):
        code = launcher.ensure_visible_role_session_for_viewer(role, mode="attach-or-start",
                                                               session_dir=launcher.role_session_dir(config, role),
                                                               pane_state_dir=state, runner=tmux)
    out["already_running"] = {"code": code, "starts": len(tmux.started()),
                              "own": files(launcher.role_session_dir(config, role), role)}
    return out


def _drop_time(path: Path, mtime: float) -> None:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    parsed.pop("updated_at")
    path.write_text(json.dumps(parsed) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


# --- the parent -------------------------------------------------------------------------------------------------------


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run_child(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd569.") as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES) and k not in ("TMUX", "TMUX_PANE")}
        env.update(HOME=str(work / "home"), PYTHONDONTWRITEBYTECODE="1")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), str(work)],
                               capture_output=True, text=True, env=env, timeout=600)
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
        with tempfile.TemporaryDirectory(prefix="syrd569-before.") as tmp:
            before_run(run_child(tree_at(BEFORE, Path(tmp) / "before")))
    now = sorted((p.name, p.stat().st_mtime_ns) for p in real.rglob("*")) if real.is_dir() else []
    check(now == untouched, f"the real account's pgu session store is untouched: {sorted(set(now) ^ set(untouched))}")
    candidate(after)
    print(f"stranded_session_record_test: {CHECKS} checks ok")
    return 0


def before_run(before: dict) -> None:
    for name, got in before["hook"].items():
        check(got["rc"] == 0 and got["launcher_reads"] == "" and got["shared"] != "" and got["restart"]["resumed"] == ""
              and got["restart"]["fresh_line"],
              f"before: {name}'s ({PROVIDERS[name]}) hook records into the shared store, and its restart starts fresh: {got}")
    otto = before["otto_only_shared"]
    check(otto["resumed"] == "" and otto["fresh_line"], f"before: otto's records in the shared store are never read: {otto}")


def candidate(after: dict) -> None:
    for n, (name, cli) in enumerate(sorted(PROVIDERS.items()), start=1):
        got = after["hook"][name]
        check(got["rc"] == 0 and got["launcher_reads"] == sid(n) and got["shared"] == "",
              f"{name}'s {cli} hook records its conversation in the role's own store, where the launcher reads it: {got}")
        restart = got["restart"]
        check(restart["code"] == 0 and restart["starts"] == 1 and restart["resumed"] == sid(n) and restart["resume_args"]
              and not restart["adopted_line"],
              f"and {name}'s next start resumes it, with {cli}'s own resume arguments, once: {restart}")
    other = after["hook_other_target"]
    check(other == {"rc": 0, "flag_dir": ["pgu-other_0.0.json"], "own_dir": []},
          f"a hook run for another target keeps to its --session-dir: {other}")
    check(after["hook_no_pane_dir"] == {"rc": 0, "flag_dir": sid(31)},
          f"and so does a pane that names no session directory: {after['hook_no_pane_dir']}")
    check(after["hook_legacy_pane_dir"] == {"rc": 0, "own": sid(32), "shared": ""},
          f"a legacy pane naming its directory as PGU_TICKET_BOARD_PANE_SESSION_DIR is followed too: {after['hook_legacy_pane_dir']}")

    for label, n, cli in (("otto_only_shared", 1, "claude"), ("otto_only_shared_agy", 2, "agy"), ("otto_only_shared_codex", 3, "codex")):
        got = after[label]
        check(got["code"] == 0 and got["starts"] == 1 and got["resumed"] == sid(n) and got["resume_args"] and got["adopted_line"]
              and got["own"] == {"record": [sid(n), NEW]} and got["shared"] == {"record": [sid(n), NEW]} and got["shared_untouched"],
              f"{label}: a {cli} conversation recorded only in the shared store is taken back and resumed; the shared record stays: {got}")
    got = after["same_id_older_copy"]
    check(got["resumed"] == sid(4) and not got["adopted_line"] and got["own"] == {"record": [sid(4), OLD]} and got["shared_untouched"],
          f"the role's own copy of the same conversation is left as it is: {got}")
    got = after["older_other_copy"]
    check(got["resumed"] == sid(5) and got["adopted_line"] and got["own"] == {"record": [sid(5), NEW], ".superseded": [sid(6), OLD]}
          and got["shared_untouched"],
          f"an older other conversation in the role's store is set aside, not lost, for the newer one: {got}")
    got = after["only_set_aside_copy"]
    check(got["resumed"] == sid(7) and got["adopted_line"] and got["own"] == {"record": [sid(7), NEW], ".superseded": [sid(7), OLD]},
          f"Main's case: a conversation set aside long ago but used since is resumed: {got}")
    got = after["own_newer"]
    check(got["resumed"] == sid(9) and not got["adopted_line"] and got["own"] == {"record": [sid(9), NEW]},
          f"the role's own newer record wins over an older shared one: {got}")
    got = after["set_aside_since"]
    check(got["resumed"] == "" and not got["adopted_line"] and got["own"] == {".superseded": [sid(11), NEW]},
          f"a shared record older than what the role set aside since is never taken back: {got}")
    got = after["other_target"]
    check(got["resumed"] == "" and not got["adopted_line"] and got["own"] == {},
          f"a shared record for another target is ignored: {got}")
    got = after["not_isolated"]
    check(got["resumed"] == sid(13) and not got["adopted_line"],
          f"a project without role-state isolation reads its one store, unchanged: {got}")
    got = after["no_updated_at_older_file"]
    check(got["resumed"] == sid(15) and got["adopted_line"],
          f"a record without a time is dated by its file: {got}")

    first, second = after["missing_conversation"]
    check(first["adopted_line"] and first["resumed"] == "" and first["code"] == 0 and first["own"] == {".superseded": [sid(20), NEW]}
          and first["shared"] == {"record": [sid(20), NEW]},
          f"a taken-back conversation the provider no longer has starts fresh, and is set aside: {first}")
    check(not second["adopted_line"] and second["resumed"] == "" and second["code"] == 0 and second["shared"] == {"record": [sid(20), NEW]},
          f"and is not taken back again at the next restart; the shared record still stays: {second}")
    got = after["attach_role"]
    check(got["code"] == 0 and got["resumed"], f"attaching a detached role resumes its stranded conversation: {got}")
    got = after["attach_running"]
    check(got["detached_before"] and got["starts"] == 0 and got["own"] == {"record": [sid(24), OLD]},
          f"attaching a running role starts nothing and takes nothing back: {got}")
    got = after["empty_shared_id"]
    check(got["resumed"] == sid(17) and not got["adopted_line"] and got["own"] == {"record": [sid(17), OLD]},
          f"a shared record with no session id is ignored: {got}")
    got = after["already_running"]
    check(got["code"] == 0 and got["starts"] == 0 and got["own"] == {},
          f"a role already running is not started again and nothing is taken back: {got}")


if __name__ == "__main__":
    raise SystemExit(main())
