#!/usr/bin/env python3
"""SYRD-546: a Hermes role with an effort starts, and Hermes runs it at that effort.

Otto's `main` (Hermes, glm-5.3, effort high; OTTO-1) was launched as `hermes -m
glm-5.3 --reasoning high --yolo --accept-hooks --pass-session-id`. Hermes 0.15.2
has no reasoning flag: it read `high` as a subcommand and exited 2 ("argument
command: invalid choice: 'high'"), so the pane closed at once. Hermes takes its
main agent's effort only from `agent.reasoning_effort` in `$HERMES_HOME/config.yaml`.

Each scenario is a disposable tenant with a Hermes role, the launcher's own
config loader, `prepare_hermes_home_for_role` and `cli_command_for_role`, run in
a child against a tree -- main `BEFORE` from a `git archive`, and this one --
with HOME and the owner's shared Hermes home inside a temporary directory. The
argv and home it produces are then handed to the installed Hermes itself, when
it is there, offline in its own user and network namespace (`unshare -rn`):
its real `main()` parses the argv with only the chat it would start stubbed
out, and its real `cli` module loads the role's config. No provider is reached.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "a1bcdade8a18bb0bf56e6bb58f01a45e3b1983fe"
HERMES_PYTHON = Path("/opt/hermes/venv/bin/python3")
CHECKS = 0
SHARED = "# the owner's own notes\nmodel:\n  default: glm-5.3\nagent:\n  max_turns: 60\ndisplay:\n  compact: true\n"

#: Runs in a child: argv is the tree, the scratch root, and a JSON list of steps, each
#: {"effort": ..., "shared": text-or-null, "own": text-or-null}, applied to one tenant in order.
CHILD = r'''
import contextlib, io, json, os, sys
from pathlib import Path
tree, root, steps = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3])
for extra in (str(tree), str(tree / "tests")):
    sys.path.insert(0, extra)
import team_launcher_presentation_test as fixture
from scripts import team_launcher

(root / "tenant").mkdir(parents=True)
config_path = fixture._write_presentation_config(root / "tenant")
raw = json.loads(config_path.read_text(encoding="utf-8"))
home = root / "home"
raw["session_dir"] = str(home / ".local" / "state" / "porter-ticket-board" / "pane-sessions")
raw["desktop_access"] = {"mode": "headless"}
shared = home / ".hermes"
shared.mkdir(parents=True)
(shared / "SOUL.md").write_text("soul\n", encoding="utf-8")
out = []
for step in steps:
    for role in raw["roles"]:
        if role["role"] == "app":
            role.update(cli=["hermes"], live_commands=["hermes"], model="glm-5.3", effort=step["effort"], yolo=True)
    config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if step.get("shared") is not None:
        (shared / "config.yaml").write_text(step["shared"], encoding="utf-8")
    config = team_launcher.load_project_config("porter", config_path)
    role = next(r for r in config.roles if r.role == "app")
    store = team_launcher.role_session_dir(config, role)
    role_home = team_launcher.hermes_home_for_role(role, session_dir=store)
    if step.get("own") is not None:
        role_home.mkdir(parents=True, exist_ok=True)
        (role_home / "config.yaml").unlink(missing_ok=True)
        (role_home / "config.yaml").write_text(step["own"], encoding="utf-8")
    before = (shared / "config.yaml").stat() if (shared / "config.yaml").exists() else None
    notices = io.StringIO()
    with contextlib.redirect_stderr(notices):
        team_launcher.prepare_hermes_home_for_role(role, session_dir=store)
    after = (shared / "config.yaml").stat() if (shared / "config.yaml").exists() else None
    command = team_launcher.cli_command_for_role(role, session_dir=store)
    at = max(i for i, word in enumerate(command) if word == "hermes")
    target = role_home / "config.yaml"
    out.append({
        "argv": command[at + 1:], "env_home": next((w.split("=", 1)[1] for w in command if w.startswith("HERMES_HOME=")), ""),
        "home": str(role_home), "shared": str(shared / "config.yaml"),
        "kind": "link" if target.is_symlink() else "file" if target.is_file() else "none",
        "points_at": os.readlink(target) if target.is_symlink() else "",
        "text": target.read_text(encoding="utf-8") if target.is_file() else "",
        "mode": oct(target.lstat().st_mode & 0o777) if target.exists() or target.is_symlink() else "",
        "shared_text": (shared / "config.yaml").read_text(encoding="utf-8") if (shared / "config.yaml").exists() else None,
        "shared_same_file": before is None or (after is not None and (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)),
        "soul": "link" if (role_home / "SOUL.md").is_symlink() else "other",
        "notices": notices.getvalue(),
    })
print(json.dumps(out))
'''


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def run(tree: Path, steps: list[dict], scratch: Path) -> list[dict]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(scratch / "home"), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-B", "-c", CHILD, str(tree), str(scratch), json.dumps(steps)], cwd=tree, env=env,
                          capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    check(done.returncode == 0, f"the scenario ran in {tree}: {done.stderr[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def hermes(script: str, *args: str, home: str) -> subprocess.CompletedProcess[str]:
    """The installed Hermes, offline: its own interpreter, HERMES_HOME the role's home."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": home, "HERMES_HOME": home}
    return subprocess.run(["unshare", "-rn", str(HERMES_PYTHON), "-I", "-c", script, *args], env=env, capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL)


#: Hermes's own `main()`, with only the chat it would start replaced by a report of what it parsed.
PARSE = r'''
import json, sys
import hermes_cli.main as m
def chat(args):
    print("PARSED " + json.dumps({k: getattr(args, k, None) for k in ("model", "yolo", "accept_hooks", "pass_session_id", "command")}), flush=True)
    raise SystemExit(0)
m.cmd_chat = chat
sys.argv = ["hermes", *sys.argv[1:]]
m.main()
'''
#: Hermes's own `cli` module loading the role's config, as a chat would.
LOAD = r'''
import json
import cli
effort = cli.CLI_CONFIG["agent"].get("reasoning_effort", "")
print("LOADED " + json.dumps({"effort": effort, "reasoning": cli._parse_reasoning_config(effort),
                              "max_turns": cli.CLI_CONFIG["agent"].get("max_turns"), "model": cli.CLI_CONFIG["model"].get("default")}), flush=True)
'''


def last(prefix: str, done: subprocess.CompletedProcess[str]):
    line = next((line for line in reversed(done.stdout.splitlines()) if line.startswith(prefix)), None)
    return json.loads(line[len(prefix):]) if line else None


def scratch() -> Path:
    path = Path(tempfile.mkdtemp(prefix="syrd546-"))
    return path


def test_main_passed_a_flag_hermes_rejects() -> None:
    if os.environ.get("SYRD546_MUTATION_SKIP_BEFORE"):
        return
    base, tmp = scratch(), scratch()
    try:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(base)], input=archive.stdout, check=True)
        got = run(base, [{"effort": "high", "shared": SHARED}], tmp)[0]
        check(got["argv"][:4] == ["-m", "glm-5.3", "--reasoning", "high"] and got["kind"] == "link",
              f"main {BEFORE[:8]}: the effort is a --reasoning flag and the role's config is the shared link: {got['argv']} {got['kind']}")
        if HERMES_PYTHON.exists():
            done = hermes(PARSE, *got["argv"], home=got["home"])
            check(done.returncode == 2 and "argument command: invalid choice: 'high'" in done.stderr,
                  f"and the installed Hermes exits 2 on it, as otto's pane did: {done.returncode} {done.stderr[-300:]}")
    finally:
        shutil.rmtree(base)
        shutil.rmtree(tmp)


def test_the_role_starts_and_hermes_runs_it_at_its_effort() -> None:
    tmp = scratch()
    try:
        got = run(ROOT, [{"effort": "high", "shared": SHARED}], tmp)[0]
        check("--reasoning" not in got["argv"] and got["argv"][:2] == ["-m", "glm-5.3"]
              and got["argv"][2:] == ["--yolo", "--accept-hooks", "--pass-session-id"], f"no effort flag, the rest as before: {got['argv']}")
        check(got["env_home"] == got["home"], f"the pane's HERMES_HOME is the prepared home: {got['env_home']}")
        check(got["kind"] == "file" and got["mode"] == "0o600" and got["text"].startswith("# Written by Switchyard for this role (SYRD-546)"),
              f"the role's config.yaml is its own, private and marked: {got['kind']} {got['mode']}")
        check(got["shared_same_file"] and got["shared_text"] == SHARED, "the owner's shared config.yaml is neither written nor replaced")
        check(got["soul"] == "link", "the other shared entries are still linked")
        check(got["notices"] == "", f"nothing to say: {got['notices']}")
        if not HERMES_PYTHON.exists():
            print(f"hermes_role_effort_test: {HERMES_PYTHON} is not installed here; the Hermes checks are SKIPPED")
            return
        parsed = hermes(PARSE, *got["argv"], home=got["home"])
        check(parsed.returncode == 0 and last("PARSED ", parsed) == {"model": "glm-5.3", "yolo": True, "accept_hooks": True,
                                                                   "pass_session_id": True, "command": None},
              f"the installed Hermes parses the pane's argv and would start its chat: {parsed.returncode} {parsed.stdout[-300:]} {parsed.stderr[-300:]}")
        loaded = hermes(LOAD, home=got["home"])
        check(loaded.returncode == 0 and last("LOADED ", loaded) == {"effort": "high", "reasoning": {"enabled": True, "effort": "high"},
                                                                   "max_turns": 60, "model": "glm-5.3"},
              f"and runs it at effort high, with the owner's other settings: {loaded.stdout[-300:]} {loaded.stderr[-300:]}")
    finally:
        shutil.rmtree(tmp)


def test_the_role_config_follows_the_effort_and_the_shared_document() -> None:
    tmp = scratch()
    try:
        edited = SHARED.replace("max_turns: 60", "max_turns: 90")
        first, changed, newer, cleared = run(ROOT, [
            {"effort": "high", "shared": SHARED},
            {"effort": "low"},
            {"effort": "low", "shared": edited},
            {"effort": ""},
        ], tmp)
        check("reasoning_effort: high" in first["text"] and "reasoning_effort: low" in changed["text"], "a changed effort is rewritten at the next start")
        check("max_turns: 90" in newer["text"] and "reasoning_effort: low" in newer["text"], "an edit to the shared document reaches the role at its next start")
        check(cleared["kind"] == "link" and cleared["points_at"] == cleared["shared"] and cleared["argv"][2:] == ["--yolo", "--accept-hooks", "--pass-session-id"],
              f"with no effort, the shared document is linked back and nothing is passed: {cleared['kind']} {cleared['points_at']}")
        check(all(step["shared_same_file"] for step in (first, changed, newer, cleared)), "the shared config is never written by Switchyard")
        none = run(ROOT, [{"effort": "", "shared": SHARED}], scratch_two := scratch())[0]
        shutil.rmtree(scratch_two)
        check(none["kind"] == "link" and none["text"] == SHARED, "a role that never had an effort keeps the link, as before")
    finally:
        shutil.rmtree(tmp)


def test_what_it_leaves_alone_and_says_why() -> None:
    for label, step, notice in (
        ("a config.yaml of the role's own", {"effort": "high", "shared": SHARED, "own": "agent:\n  reasoning_effort: low\n"}, "has a config.yaml of its own"),
        ("a shared document that is not a mapping", {"effort": "high", "shared": "- a list\n"}, "is not a YAML mapping"),
        ("a shared document that is not YAML", {"effort": "high", "shared": "agent: [unclosed\n"}, "cannot read"),
    ):
        tmp = scratch()
        try:
            got = run(ROOT, [step], tmp)[0]
            check(notice in got["notices"] and "--reasoning" not in got["argv"],
                  f"{label}: the role still starts, and the launcher says why its effort is not applied: {got['notices']!r}")
            if "own" in step:
                check(got["kind"] == "file" and got["text"] == step["own"], f"{label}: left exactly as it was")
            else:
                check(got["kind"] == "link", f"{label}: the shared document stays linked: {got['kind']}")
        finally:
            shutil.rmtree(tmp)
    tmp = scratch()
    try:
        got = run(ROOT, [{"effort": "high", "shared": None}], tmp)[0]
        check(got["kind"] == "file" and got["text"].endswith("agent:\n  reasoning_effort: high\n"),
              f"with no shared config.yaml at all, the role's carries only its effort: {got['text']!r}")
    finally:
        shutil.rmtree(tmp)


def main() -> int:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
    print(f"hermes_role_effort_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
