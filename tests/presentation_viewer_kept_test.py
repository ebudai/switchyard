#!/usr/bin/env python3
"""SYRD-749 on real tmux: a launch over a viewer a person is watching keeps it, and their terminal stays attached.

SYRD-653's clean-host run, viewer layout: a terminal outside presentation was attached to `pie-viewer`, and a healthy
`sudo switchyard resume-provision pie` ended it. `_launch_viewer` killed and rebuilt the viewer session on every
launch, whoever was watching it.

Every worker is a real tmux session on a private server; the display slots and the viewer are the ones the product
builds. A person's window on the viewer is a real terminal: a pty running `tmux attach -t =<slug>-viewer`. The launch is
the real `launch_presentation(layout="viewer")`:
- nobody watching: the viewer is built, as before;
- watched: the same viewer session and panes are kept, the terminal stays attached, and the workers are untouched;
  run again, still kept;
- a slot's role changes while watched: the kept viewer's label follows;
- watched but no longer showing every slot: rebuilt, as before;
- watched no more: rebuilt, as before;
- an explicit bootstrap's call still rebuilds.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from presentation_split_title_test import Split  # noqa: E402

PROJECT = "demo749"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def main() -> int:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from scripts import presentation_controller as pc
    from scripts import presentation_viewer as pv
    from scripts import team_launcher as tl

    tmux_bin = shutil.which("tmux")
    assert tmux_bin, "tmux is required"
    work = Path(tempfile.mkdtemp(prefix="s749-", dir="/tmp"))
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    os.environ["TMUX_TMPDIR"] = str(work)
    socket = work / f"tmux-{os.getuid()}" / "default"
    socket.parent.mkdir(mode=0o700)
    probe = subprocess.run([tmux_bin, "-S", str(socket), "list-sessions"], capture_output=True, text=True)
    assert probe.returncode != 0, f"refusing to run: a server already answers on {socket}"

    def tmux(*args: str, check_rc: bool = True) -> str:
        proc = subprocess.run([tmux_bin, "-S", str(socket), *args], capture_output=True, text=True)
        if check_rc and proc.returncode != 0:
            raise AssertionError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    me = tl.current_user_name()
    roles = [{"role": "director", "slot": 0}, {"role": "main", "slot": 1}, {"role": "app", "slot": 2}]
    for role in roles:
        role.update({"cli": "claude", "workdir": str(work / "work" / role["role"])})
        Path(role["workdir"]).mkdir(parents=True)
    config_path = work / f"{PROJECT}.json"
    config_path.write_text(json.dumps({"project": PROJECT, "project_name": "Demo 749", "desktop_access": {"mode": "headless"},
                                       "board_url": "http://127.0.0.1:1/", "run_as_user": me, "roles": roles}),
                           encoding="utf-8")
    config = tl.load_project_config(PROJECT, config_path)
    state_path = work / "presentation.json"
    viewer = tl.viewer_session_for_project(PROJECT)
    env = {**os.environ, "TERM": "xterm-256color"}
    people: list[Split] = []

    def launch() -> str:
        said = io.StringIO()
        with redirect_stdout(said):
            result = pc.launch_presentation(config, config_path=config_path, layout="viewer", state_path=state_path,
                                            runner=subprocess.run, print_func=print)
        check(result == 0, f"the launch's presentation step answers 0: {result}")
        return said.getvalue()

    def viewer_identity() -> str:
        return tmux("list-panes", "-t", f"={viewer}:0", "-F", "#{session_id} #{pane_id} #{pane_pid}", check_rc=False)

    def watchers() -> list[str]:
        out = tmux("list-clients", "-F", "#{client_tty}\t#{client_session}", check_rc=False)
        return sorted(line.split("\t")[0] for line in out.splitlines() if line.endswith(f"\t{viewer}"))

    def workers() -> str:
        return "\n".join(tmux("display-message", "-p", "-t", f"={PROJECT}-{r['role']}:0.0",
                              "#{session_id} #{pane_id} #{pane_pid}") for r in roles)

    def settle(want: int) -> list[str]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(watchers()) != want:
            time.sleep(0.1)
        return watchers()

    def label(slot: int) -> str:
        return tmux("display-message", "-p", "-t", f"={viewer}:0.{slot}", "#{@switchyard_role}", check_rc=False)

    try:
        for role in roles:
            tmux("new-session", "-d", "-x", "120", "-y", "40", "-s", f"{PROJECT}-{role['role']}", "sleep 100000")
        pc.reconnect_display_slots(config, config_path=config_path, state_path=state_path, runner=subprocess.run)

        said = launch()
        built = viewer_identity()
        check(len(built.splitlines()) == 3 and "keeping it" not in said, f"nobody watching: the viewer is built: {built}")
        workers_before = workers()

        # A person opens the viewer in their terminal.
        people.append(Split([tmux_bin, "attach", "-t", f"={viewer}"], env))
        person = settle(1)
        check(len(person) == 1, f"fixture: a terminal outside presentation is attached to the viewer: {person}")

        # `sudo switchyard resume-provision` over the healthy project: the same launch, the viewer watched.
        said = launch()
        check(viewer_identity() == built and watchers() == person,
              f"watched: the same viewer session, panes and processes, and the person's terminal still attached: "
              f"{viewer_identity()} {built} {watchers()} {person}")
        check(f"switchyard: {PROJECT}'s viewer is open in 1 terminal; keeping it, with its slots refreshed in place" in said,
              f"and says so: {said}")
        check(workers() == workers_before, f"the workers are the ones they were: {workers()}")
        said = launch()
        check(viewer_identity() == built and watchers() == person and "keeping it" in said, "run again, still kept")

        # A slot's role changes while the viewer is watched: the kept viewer's label follows.
        pc.presentation_action(config, config_path=config_path, action="hide", slot=2, state_path=state_path,
                               environ={"TICKET_BOARD_CALLER_ROLE": "director"}, runner=subprocess.run)
        launch()
        check(viewer_identity() == built and label(2) == "hidden" and label(0) == "director",
              f"a hidden slot is labelled hidden on the kept viewer: {label(0)} {label(2)}")

        # Watched, but one of its panes shows another slot: rebuilt, as before.
        from scripts.presentation_display_session import display_session_name

        tmux("respawn-pane", "-k", "-t", f"={viewer}:0.1", pv._viewer_observer_attach(display_session_name(PROJECT, 0)))
        launch()
        rebuilt = viewer_identity()
        check(len(rebuilt.splitlines()) == 3 and rebuilt.split()[0] != built.split()[0],
              f"a viewer that no longer shows each of its slots is rebuilt whole, even though it was watched: {rebuilt}")

        # Watched, and one of its panes respawned to a one-word command that is no viewer pane at all: the launch
        # returns at once and rebuilds it (Audit of c2ae3f28: such a pane hung the launch).
        for split in people:
            split.close()
        people.clear()
        settle(0)
        people.append(Split([tmux_bin, "attach", "-t", f"={viewer}"], env))
        check(len(settle(1)) == 1, "fixture: watched again")
        tmux("respawn-pane", "-k", "-t", f"={viewer}:0.1", "cat")
        check(tmux("display-message", "-p", "-t", f"={viewer}:0.1", "#{pane_start_command}").strip('"') == "cat",
              "fixture: tmux reports the pane's command as the one word `cat`")
        before = viewer_identity()
        finished: list[str] = []
        worker = threading.Thread(target=lambda: finished.append(launch()), daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(30)
        took = time.monotonic() - started
        check(finished and took < 20, f"the launch returns promptly: {took:.1f}s, finished={bool(finished)}")
        after = viewer_identity()
        check(len(after.splitlines()) == 3 and after.split()[0] != before.split()[0] and "keeping it" not in finished[0],
              f"a pane that is no viewer pane is not showing its slot: the viewer is rebuilt: {after}")
        check(workers() == workers_before, "and the workers are untouched")

        # Watched no more: rebuilt, as before.
        for split in people:
            split.close()
        people.clear()
        settle(0)
        before = viewer_identity()
        launch()
        check(viewer_identity().split()[0] != before.split()[0], "nobody watching: rebuilt, as before")
        check(workers() == workers_before, "the workers are untouched throughout")

        # An explicit bootstrap is asked to build the viewer: its call does not keep one, and the default rebuilds.
        people.append(Split([tmux_bin, "attach", "-t", f"={viewer}"], env))
        settle(1)
        before = viewer_identity()
        state = pc._read_state(state_path, config=config, config_path=config_path)
        pv._launch_viewer(config, state, runner=subprocess.run)
        check(viewer_identity().split()[0] != before.split()[0], "the default call rebuilds, watched or not")
        action = ast.parse(textwrap.dedent(inspect.getsource(pc.presentation_action)))
        bootstrap = [[kw.arg for kw in node.keywords] for node in ast.walk(action) if isinstance(node, ast.Call)
                     and getattr(node.func, "id", "") == "_launch_viewer"]
        launch_calls = [[kw.arg for kw in node.keywords] for node in ast.walk(ast.parse(textwrap.dedent(
            inspect.getsource(pc.launch_presentation)))) if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "_launch_viewer"]
        check(bootstrap and all("keep_if_watched" not in kws for kws in bootstrap)
              and launch_calls and all("keep_if_watched" in kws for kws in launch_calls),
              f"only the ordinary launch keeps a watched viewer: bootstrap {bootstrap}, launch {launch_calls}")
    finally:
        for split in people:
            split.close()
        if socket.exists():
            tmux("kill-server", check_rc=False)
        shutil.rmtree(work, ignore_errors=True)
    print(f"presentation_viewer_kept_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
