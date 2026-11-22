#!/usr/bin/env python3
"""SYRD-785: one Ctrl-C aborts `switchyard new`, the first-run sign-in and trust steps included, and says what it left.

The User, aborting test13: "I had to press ctrl-c a LOT". The first-run steps hold the terminal raw, so Ctrl-C was a
byte passed on to the provider; Claude takes it as "press again to exit", so each press ended at most that one step and
the phase went on to the next -- a sign-in, then a folder-trust step per role.

Here the real first-run phase (`first_run_auth.run_first_run_auth_phase`) runs in a child on a real pty, its terminal
held raw by the real watched session, with one sign-in step and three folder-trust steps. Each step's provider is a
stand-in that, like Claude, ignores the first SIGINT and answers a Ctrl-C byte with "press Ctrl-C again to exit"; it
starts a grandchild in its own process group, and records its pid, group and grandchild. One Ctrl-C is typed:
* the phase ends -- the child reports the interrupt, not a finished phase;
* no later step starts;
* the stand-in's whole process group is gone, the grandchild included.
And `switchyard_new_command` itself, aborted at each point, ends with 130 and one message naming the slug and the
teardown commands.
"""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import setup_abort, team_launcher  # noqa: E402

CHECKS = 0

#: A provider that will not let go of the first Ctrl-C, as Claude does, and keeps asking a question so the step never
#: finishes by itself.
STAND_IN = r'''
import os, signal, subprocess, sys, termios, time, tty
record = sys.argv[1]
signal.signal(signal.SIGINT, lambda *_: print("\r\npress Ctrl-C again to exit", flush=True))
# A grandchild deaf to everything but SIGKILL -- hangups, terminate, interrupt -- as a provider's helper can be.
grandchild = subprocess.Popen([sys.executable, "-c", "import signal, time\n"
                               "[signal.signal(s, signal.SIG_IGN) for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)]\n"
                               "time.sleep(300)\n"])
with open(record, "w") as fh:
    fh.write(f"{os.getpid()} {os.getpgid(0)} {grandchild.pid}\n")
try:
    tty.setraw(0)
except termios.error:
    pass
# The keyboard protocols Claude switches on in the person's terminal (tests/fixtures/claude-first-run): kitty's
# disambiguate flags and xterm's modifyOtherKeys 2. In a terminal that honours them, Ctrl-C is an escape sequence.
print("\x1b[>1u\x1b[>4;2m", end="", flush=True)
print("\x1b[2J\x1b[HDo you trust the files in this folder?\r\n1. Yes, proceed\r\n2. No, exit\r\n", flush=True)
while True:
    data = os.read(0, 1024)
    if b"\x03" in data:
        print("\r\npress Ctrl-C again to exit", flush=True)
    time.sleep(0.05)
'''

HARNESS = r'''
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from scripts import first_run_setup, team_launcher
work = Path(sys.argv[2])
stand_in = work / "stand_in.py"
def step_command(name):
    return (sys.executable, str(stand_in), str(work / f"{name}.started"))
manifest = first_run_setup.FirstRunSetupManifest(
    owner_user="syrd785-owner",
    login_steps=[first_run_setup.FirstRunAuthLoginStep("claude", ("director",), step_command("sign-in"))],
    folder_trust_steps=[first_run_setup.FirstRunFolderTrustStep("claude", role, work, step_command(f"trust-{role}"),
                                                                (role,)) for role in ("director", "main", "ops")],
    stale_codex_hook_trust=[], missing_cli_roles={})
team_launcher.build_first_run_setup_manifest = lambda *a, **k: manifest
team_launcher.print_first_run_setup_manifest = lambda *a, **k: None
team_launcher._cli_auth_status = lambda *a, **k: "unauthenticated"
team_launcher._workdir_is_trusted = lambda *a, **k: False
team_launcher._owner_command_env_args = lambda owner, home, command: list(command)
team_launcher._owner_home_for_auth = lambda owner: work
config = SimpleNamespace(project="p785", run_as_user="syrd785-owner", desktop_access=None, roles=[])
try:
    team_launcher.run_first_run_auth_phase(config, owner_user="syrd785-owner", owner_home=work,
                                           print_func=lambda line: None)
    print("\r\nRESULT finished", flush=True)
except KeyboardInterrupt as stop:
    print(f"\r\nRESULT interrupted {type(stop).__name__}", flush=True)
'''


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = Path(f"/proc/{pid}/stat")
    try:
        return status.read_text().split()[2] != "Z"
    except OSError:
        return False


#: Ctrl-C as a terminal sends it: plainly, under xterm's modifyOtherKeys, and under the kitty keyboard protocol (as
#: tmux with extended-keys on, or Konsole with its kitty keyboard enabled, sends it once Claude switches them on).
CTRL_C_KEYS = {"0x03": b"\x03", "modifyOtherKeys": b"\x1b[27;5;99~", "kitty": b"\x1b[99;5u"}


def test_one_ctrl_c_ends_the_first_run_phase() -> None:
    for label, key in CTRL_C_KEYS.items():
        phase_after_one(label, key)


def phase_after_one(label: str, key: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="syrd785.") as raw:
        work = Path(raw)
        (work / "stand_in.py").write_text(STAND_IN, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, "-c", HARNESS, str(ROOT), str(work)], stdin=slave, stdout=slave,
                                stderr=slave, env=env, cwd=str(work), start_new_session=True)
        os.close(slave)
        out, typed, deadline = [], False, time.monotonic() + 60
        started = work / "sign-in.started"
        while proc.poll() is None and time.monotonic() < deadline:
            ready, _w, _x = select.select([master], [], [], 0.2)
            if ready:
                try:
                    out.append(os.read(master, 65536).decode("utf-8", "replace"))
                except OSError:
                    pass
            if not typed and started.exists() and "Do you trust" in "".join(out):
                time.sleep(0.5)  # the session is relaying by now
                os.write(master, key)
                typed = True
        if proc.poll() is None:
            proc.kill()
        try:
            while True:
                ready, _w, _x = select.select([master], [], [], 0.2)
                if not ready:
                    break
                out.append(os.read(master, 65536).decode("utf-8", "replace"))
        except OSError:
            pass
        os.close(master)
        text = "".join(out)
        check(typed, f"{label}: the sign-in step started and Ctrl-C was typed once: {text[-600:]}")
        check("RESULT interrupted SetupInterrupted" in text,
              f"{label}: one Ctrl-C ended the whole phase, as an interrupt: {text[-800:]}")
        later = sorted(path.name for path in work.glob("*.started") if path != started)
        check(later == [], f"{label}: no later step started: {later}")
        pid, pgid, grandchild = (int(value) for value in started.read_text().split())
        time.sleep(0.2)
        check(pgid == pid and not alive(pid) and not alive(grandchild),
              f"{label}: the provider's whole process group is gone, the grandchild included: {pid} {pgid} {grandchild}")


def test_the_session_passes_on_what_came_before_and_raises() -> None:
    check(setup_abort.interrupt_in(b"ab\x03cd") == (b"ab", True) and setup_abort.interrupt_in(b"abc") == (b"abc", False),
          "what came before Ctrl-C is passed on, nothing after it")
    for key in (b"\x1b[27;5;99~", b"\x1b[99;5u", b"\x1b[99;5:1u", b"\x1b[99;69u", b"\x1b[99;133u", b"\x1b[27;69;99~"):
        check(setup_abort.interrupt_in(b"ab" + key + b"cd") == (b"ab", True),
              f"{key!r}: Ctrl-C in its encoding, lock bits ignored, and only what came before passed on")
    for key in (b"\x1b[99;5:3u", b"\x1b[99;6u", b"\x1b[27;6;99~", b"\x1b[99;3u", b"\x1b[27;5;100~", b"\x1b[A",
                b"\x1b[99;5:2u"):
        check(setup_abort.interrupt_in(b"a" + key) == (b"a" + key, False),
              f"{key!r}: a release, Ctrl+Shift+C, Alt+C, another key or an arrow is not Ctrl-C, and goes on whole")
    scanner = setup_abort.InterruptScanner()
    first, found_first = scanner.feed(b"xy\x1b[99;")
    second, found_second = scanner.feed(b"5u")
    check((first, found_first, second, found_second) == (b"xy", False, b"", True),
          f"a sequence a read cut short is held, not relayed half-way, and recognised whole: {first!r} {second!r}")
    scanner = setup_abort.InterruptScanner()
    passed, _found = scanner.feed(b"q\x1b")
    check(passed == b"q" and scanner.release() == b"\x1b",
          "a lone Escape is held only until nothing more comes, then goes on")
    scanner = setup_abort.InterruptScanner()
    passed, _found = scanner.feed(b"\x1b")
    later, found = scanner.feed(b"OA")
    check((passed, later, found) == (b"", b"\x1bOA", False), "an Escape that begins another kind of key goes on with it")
    check(issubclass(setup_abort.SetupInterrupted, KeyboardInterrupt) and not issubclass(setup_abort.SetupInterrupted,
                                                                                         Exception),
          "an interrupt no step's `except Exception` swallows")


def test_stopping_a_group_that_ignores_sigint() -> None:
    proc = subprocess.Popen([sys.executable, "-c",
                             "import signal, subprocess, time\n"
                             "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                             "subprocess.Popen(['sleep', '300']); time.sleep(300)\n"],
                            start_new_session=True)
    time.sleep(0.3)
    began = time.monotonic()
    setup_abort.stop_process_group(proc, signals=((signal.SIGINT, 0.3), (signal.SIGTERM, 0.3),
                                                  (signal.SIGKILL, 1.0)))
    gone = True
    try:
        os.killpg(proc.pid, 0)
        gone = False
    except ProcessLookupError:
        pass
    check(gone and proc.returncode is not None and time.monotonic() - began < 5,
          f"a group deaf to SIGINT and SIGTERM still ends, bounded: {proc.returncode}")


class Stages:
    names = team_launcher.NEW_PROJECT_STAGES

    def __init__(self, current: str | None) -> None:
        self.current = (current, 0.0) if current else None


def new_aborted_at(stage: str | None, *, before_choices: bool = False) -> tuple[int, list[str]]:
    """The real `switchyard_new_command`, its phases stood in for, interrupted at `stage`, and what `scripts/switchyard`
    then says, through the very handler it calls."""

    def choices(**_kwargs):
        if before_choices:
            raise KeyboardInterrupt
        return SimpleNamespace(**{name: None for name in (
            "agy_source_origin", "artifact_path", "design_document", "director_onboarding", "first_run_runner",
            "include_audit", "include_designer", "owner_shell", "owner_user", "project_dir",
            "resolved_agy_credential_source", "resolved_project_name", "runner", "selected_audit_roles",
            "selected_desktop_policy", "selected_implementer_roles", "selected_role_clis", "selected_role_efforts",
            "selected_role_models", "selected_router", "selected_documentation", "selected_research", "selected_plan", "upstream_report")},
            resolved_slug="test13", stages=Stages(stage))

    def interrupted(**_kwargs):
        raise setup_abort.SetupInterrupted("Ctrl-C")

    caught: BaseException | None = None
    with patch.object(team_launcher, "_resolve_new_project_choices", choices), \
            patch.object(team_launcher, "_check_new_project_preflight", interrupted):
        try:
            team_launcher.switchyard_new_command(print_func=lambda _line: None)
        except KeyboardInterrupt as stop:
            caught = stop
    check(caught is not None, "the interrupt ends the command: nothing catches it on the way out")
    message, code = setup_abort.interrupted(["new", "--yes"], caught)
    return code, [message]


def test_switchyard_new_says_what_it_left() -> None:
    code, said = new_aborted_at(None, before_choices=True)
    check(code == 130 and said == ["switchyard: new aborted (Ctrl-C) during its questions; nothing was created."],
          f"during the questions: nothing created: {code} {said}")
    code, said = new_aborted_at("host and agent CLI checks")
    check(code == 130 and len(said) == 1 and "none of test13's accounts, files or board were created" in said[0]
          and "sudo switchyard teardown test13 --dry-run" in said[0],
          f"before anything is created: said so, with the check to run if unsure: {said}")
    for stage in ("project accounts and files", "database and board", "provider sign-in and folder trust"):
        code, said = new_aborted_at(stage)
        check(code == 130 and len(said) == 1 and f"new test13 aborted (Ctrl-C) during {stage}" in said[0]
              and "partly created and was not started" in said[0]
              and "`sudo switchyard teardown test13 --dry-run`" in said[0]
              and "and `sudo switchyard teardown test13` removes it" in said[0],  # SYRD-786: no flags needed
              f"{stage}: what it left, and the exact commands that list and remove it: {said}")


def test_the_entry_point_ends_any_command_without_a_traceback() -> None:
    message, code = setup_abort.interrupted(["add-role", "p"], setup_abort.SetupInterrupted("Ctrl-C"))
    check(code == 130 and message == "switchyard: add-role aborted (Ctrl-C); nothing after the step it was in ran.",
          f"any other command: 130 and one line: {code} {message!r}")
    source = (ROOT / "scripts" / "switchyard").read_text(encoding="utf-8")
    check("except KeyboardInterrupt as interrupt:" in source and "interrupted(arguments, interrupt)" in source,
          "scripts/switchyard says it for every command, `switchyard login` and `add-role` among them")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"setup_ctrl_c_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
