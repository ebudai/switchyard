#!/usr/bin/env python3
"""SYRD-239 live UAT, third failure: recover the Director's display, and ONLY it.

After the declared-runtime repair, `switchyard recover-display mefp` exited 0
and said the Director was recovered. On the User's four-pane window the
Director was not restored, and Ops -- which had been fine -- turned into an
inert "mefp: display slot hidden" screen. The command's own report called both
slots connected.

Two causes, both reproduced here on a real tmux server:

* Recovery re-applied the WHOLE stored mapping, respawning every slot's proxy.
  While the layout is "default" that mapping is re-derived from the current
  config on every read, so after mefp's role set changed it named Director slot
  1 and Ops slot 5 of 6, while the open window still showed display 0-3 as
  they were launched. Re-applying it rewired the visible panes.
* "connected" meant the `@switchyard_role` label, which is set right after a
  respawn whether or not the attach inside it lived.

So this builds that shape for real -- four display sessions launched in the old
order with real nested attaches, four real outer clients standing in for the
Konsole panes, and a config whose derived mapping disagrees -- then closes the
Director's attachment, recovers through the real `presentation_action`, and
checks what tmux itself says afterwards.

The tmux server is isolated: TMUX is removed and TMUX_TMPDIR points into a
temporary directory, and the socket path is asserted before anything is
created or killed. A bare tmux call from a Switchyard pane otherwise drives the
LIVE server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import presentation_controller as pc  # noqa: E402
import team_launcher as tl  # noqa: E402

CHECKS = 0
PROJECT = "demo"
#: What the open window was launched with: display slot -> role.
OPENED = {0: "director", 1: "main", 2: "audit", 3: "ops"}


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tmux(*args: str, check_rc: bool = True) -> str:
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check_rc and proc.returncode != 0:
        raise AssertionError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def wait_for(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def display(slot: int) -> str:
    return f"{PROJECT}-display-{slot}"


def pane(slot: int, fmt: str) -> str:
    return tmux("display-message", "-p", "-t", f"={display(slot)}:0.0", fmt)


def clients_of(session: str) -> set[str]:
    out = tmux("list-clients", "-t", f"={session}", "-F", "#{client_tty}", check_rc=False)
    return {line for line in out.splitlines() if line}


def nested_attached(slot: int, role: str) -> bool:
    """Independent of the code under test: the slot's pane tty is a client of the role."""
    return pane(slot, "#{pane_dead}") == "0" and pane(slot, "#{pane_tty}") in clients_of(f"{PROJECT}-{role}")


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.outer: list[subprocess.Popen] = []

    def config(self) -> tuple[tl.ProjectConfig, Path]:
        """The CURRENT config: its default mapping puts director in slot 1 and
        ops in slot 5 of 6, unlike the window that is open."""
        roles = []
        for slot, name in enumerate(("designer", "director", "main", "audit", "app", "ops")):
            workdir = self.root / "work" / name
            workdir.mkdir(parents=True, exist_ok=True)
            roles.append({"role": name, "cli": "claude", "slot": slot, "workdir": str(workdir)})
        path = self.root / f"{PROJECT}.json"
        path.write_text(json.dumps({
            "project": PROJECT,
            "desktop_access": {"mode": "headless"},
            "board_url": "http://127.0.0.1:1/",
            "run_as_user": tl.current_user_name(),
            "roles": roles,
        }), encoding="utf-8")
        return tl.load_project_config(PROJECT, path), path

    def launch(self, config: tl.ProjectConfig) -> None:
        for name in ("designer", "director", "main", "audit", "app", "ops"):
            tmux("new-session", "-d", "-x", "80", "-y", "24", "-s", f"{PROJECT}-{name}", "sleep 100000")
        for slot, role in OPENED.items():
            pc._configure_display_session(config, slot, role, runner=subprocess.run, presentation_ttys=set())
        for slot, role in OPENED.items():
            wait_for(lambda s=slot, r=role: nested_attached(s, r), f"slot {slot} to attach {role}")
        # The Konsole panes: one real outer client on each display session.
        for slot in OPENED:
            self.outer.append(subprocess.Popen(
                ["script", "-qfc", f"tmux attach -t ={display(slot)}", "/dev/null"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "TERM": "xterm-256color"},
            ))
        for slot in OPENED:
            wait_for(lambda s=slot: clients_of(display(s)), f"an outer client on {display(slot)}")

    def close(self) -> None:
        for proc in self.outer:
            proc.kill()
            proc.wait()


def isolate() -> Path:
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    tmpdir = Path(tempfile.mkdtemp(prefix="sy239-", dir="/tmp"))
    os.environ["TMUX_TMPDIR"] = str(tmpdir)
    expected = tmpdir / f"tmux-{os.getuid()}" / "default"
    # Before anything is created: tmux names the socket it would use.
    probe = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
    assert probe.returncode != 0 and str(expected) in probe.stderr, (
        f"refusing to run: tmux would not use the isolated socket {expected}: {probe.stderr!r}"
    )
    return expected


def kill_isolated_server(expected: Path) -> None:
    # Only the server whose socket is the isolated one.
    socket = tmux("display-message", "-p", "#{socket_path}", check_rc=False)
    if socket == str(expected):
        tmux("kill-server", check_rc=False)


def recover(config, config_path, state_path, *, expect_start: bool = True):
    """The real presentation_action recover, as the Director's pane would call it.

    The two seams replaced are the launcher's desktop preparation and its
    worker start: the workers here are already live, which is the case this
    ticket is about.
    """
    # The module presentation_controller actually calls: `scripts.team_launcher`
    # is a different object from the `team_launcher` imported above, and a
    # patch on the wrong one let the real worker start run -- "no recorded
    # session id for director; starting fresh session" -- in the first draft.
    launcher = pc.team_launcher
    started: list[str] = []
    saved = (launcher.prepare_project_desktop, launcher.ensure_visible_role_session_for_viewer)
    launcher.prepare_project_desktop = lambda cfg, **_k: cfg
    launcher.ensure_visible_role_session_for_viewer = (
        lambda role, *_a, **_k: started.append(role.role) or 0
    )
    try:
        return pc.presentation_action(
            config, config_path=config_path, action="recover", role_name="director",
            state_path=state_path, environ={"TICKET_BOARD_CALLER_ROLE": "director"},
            runner=subprocess.run,
        )
    finally:
        launcher.prepare_project_desktop, launcher.ensure_visible_role_session_for_viewer = saved
        wanted = ["director"] if expect_start else []
        assert started == wanted, f"the worker step ran for {started}, expected {wanted}"


def test_only_the_directors_display_is_recovered_and_proven(h: Harness) -> None:
    config, config_path = h.config()
    state_path = h.root / "presentation.json"
    h.launch(config)

    # The shape that broke mefp: what is stored disagrees with what is open.
    stored = pc._read_state(state_path, config=config, config_path=config_path)["slots"]
    check(stored.get("1") == "director" and stored.get("5") == "ops",
          f"the derived mapping names director 1 / ops 5: {stored}")

    before = {slot: pane(slot, "#{pane_pid}") for slot in OPENED}
    outer_before = {slot: clients_of(display(slot)) for slot in OPENED}

    # The Director's display attachment closes; its worker lives on.
    tmux("detach-client", "-t", pane(0, "#{pane_tty}"))
    wait_for(lambda: not nested_attached(0, "director"), "the director attachment to close")
    check(tmux("has-session", "-t", f"={PROJECT}-director", check_rc=False) == "" , "the worker is alive")

    report = pc.presentation_report(config, config_path=config_path, state_path=state_path,
                                    runner=subprocess.run)
    slot0 = next(item for item in report["slots"] if item["slot"] == 0)
    check(slot0["client_state"] == "detached",
          f"the report no longer calls a closed attachment connected: {slot0}")

    state = recover(config, config_path, state_path)

    # The Director is back, in the display the window shows it in.
    check(nested_attached(0, "director"), "display 0 is a live client of the Director again")
    # Nothing else was touched: same proxy processes, same attachments, same labels.
    for slot, role in OPENED.items():
        if slot == 0:
            continue
        check(pane(slot, "#{pane_pid}") == before[slot],
              f"slot {slot} ({role}) was not respawned: {before[slot]} -> {pane(slot, '#{pane_pid}')}")
        check(nested_attached(slot, role), f"slot {slot} still shows {role}")
        check(pane(slot, "#{@switchyard_role}") == role, f"slot {slot} still labelled {role}")
    check(pane(3, "#{@switchyard_role}") == "ops", "Ops in particular is untouched")
    # The window's own clients are all still there.
    for slot in OPENED:
        check(clients_of(display(slot)) == outer_before[slot],
              f"the window pane on {display(slot)} is still attached")
    # Recorded, without rewriting any slot.
    last = state["history"][-1]
    check(last["action"] == "recover" and last["actor"] == "director" and last["detail"]["slots"] == [0],
          f"the recovery is recorded for the display it touched: {last}")
    check(pc._read_state(state_path, config=config, config_path=config_path)["slots"] == stored,
          "and the mapping itself was not changed")

    report = pc.presentation_report(config, config_path=config_path, state_path=state_path,
                                    runner=subprocess.run)
    measured = {item["slot"]: item["client_state"] for item in report["slots"] if item["slot"] in OPENED}
    check(all(value == "connected" for value in measured.values()),
          f"and the report, now measured, agrees: {measured}")


def test_a_recovery_that_does_not_attach_is_not_reported_as_one(h: Harness) -> None:
    config, config_path = h.config()
    state_path = h.root / "presentation.json"
    state_before = pc._read_state(state_path, config=config, config_path=config_path)
    history_before = list(state_before["history"])
    before = {slot: pane(slot, "#{pane_pid}") for slot in (1, 2, 3)}

    # The worker behind the Director's display is gone, and nothing restarts it.
    tmux("kill-session", "-t", f"={PROJECT}-director")
    try:
        recover(config, config_path, state_path)
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError("a recovery that could not attach was reported as success")
    check("did not attach to its worker" in message, f"it says so: {message}")
    check("nothing was recorded" in message, f"and that nothing was recorded: {message}")
    after = pc._read_state(state_path, config=config, config_path=config_path)
    check(after["history"] == history_before, "no recovery was written into the history")
    for slot in (1, 2, 3):
        check(pane(slot, "#{pane_pid}") == before[slot], f"slot {slot} was not touched")


def test_no_live_director_display_is_refused_not_invented(h: Harness) -> None:
    config, config_path = h.config()
    state_path = h.root / "presentation.json"
    # Relabel slot 0 as hidden, as if the window had no Director pane at all.
    pc._configure_display_session(config, 0, None, runner=subprocess.run, presentation_ttys=set())
    before = {slot: pane(slot, "#{pane_pid}") for slot in OPENED}
    try:
        # Refused before the worker step: nothing is started on the way.
        recover(config, config_path, state_path, expect_start=False)
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError("a recovery with no director display was reported as success")
    check("no live display of demo is showing director" in message, f"it says what is shown: {message}")
    check("slot 3: ops" in message, f"naming the live displays: {message}")
    check("slot 0: nothing" in message, f"a hidden display shows nothing, not a role: {message}")
    for slot in OPENED:
        check(pane(slot, "#{pane_pid}") == before[slot], f"slot {slot} was not touched")


def main() -> int:
    if not shutil.which("tmux") or not shutil.which("script"):
        raise SystemExit("display_recovery_live_tmux_test: tmux and script are required")
    expected = isolate()
    root = Path(tempfile.mkdtemp(prefix="sy239-work-"))
    h = Harness(root)
    try:
        for case in (
            test_only_the_directors_display_is_recovered_and_proven,
            test_a_recovery_that_does_not_attach_is_not_reported_as_one,
            test_no_live_director_display_is_refused_not_invented,
        ):
            case(h)
            print(f"  {case.__name__}: ok", flush=True)
    finally:
        h.close()
        kill_isolated_server(expected)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(expected.parent.parent, ignore_errors=True)
    print(f"display_recovery_live_tmux_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
