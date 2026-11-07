#!/usr/bin/env python3
"""SYRD-743 on real tmux: a launch over a project already on screen opens no second presentation window.

Live, on the disposable `test9` tenant: `switchyard test9` opened the project window, then `sudo switchyard
resume-provision test9` on the healthy project opened a second one beside it. Both stayed open. A resume starts its
roles through the ordinary launch, and the launch's presentation step opened a window every time it ran: nothing asked
whether a terminal was already showing the project.

Every worker is a real tmux session on a private server, and the display slots are the ones the product builds. A window
is what Konsole makes of a layout: one real terminal per slot, a pty running the shipped `switchyard-pane-window` with
the attach a launch gives it. Only starting the terminal stands in, and its stand-in opens exactly those tabs, so a
window that is opened is really on the server afterwards. The presentation step is the real `launch_presentation`:
- with no window, it opens one;
- with the window open, it opens none, says so, and the window's tabs, the workers and their sessions are as they were;
- run again, it still opens none;
- for a tenant owned by another account, run by another (root, for `resume-provision`), it asks as the owner;
- closed, the window is opened again, once;
- an explicit `bootstrap` still opens a window when asked to, and a bridged launch still hands its window back.
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
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from presentation_split_title_test import Split  # noqa: E402

PROJECT = "demo743"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def main() -> int:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from scripts import presentation_controller as pc
    from scripts import team_launcher as tl
    from scripts.presentation_window_launch import display_attach_args_for
    from scripts.presentation_window_visibility import external_presentation_clients

    tmux_bin = shutil.which("tmux")
    assert tmux_bin, "tmux is required"
    work = Path(tempfile.mkdtemp(prefix="s743-", dir="/tmp"))
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
    roles = [{"role": "director", "slot": 0}, {"role": "main", "slot": 1}]
    for role in roles:
        role.update({"cli": "claude", "workdir": str(work / "work" / role["role"])})
        Path(role["workdir"]).mkdir(parents=True)

    def configured(owner: str) -> tuple:
        path = work / f"{PROJECT}-{owner}.json"
        path.write_text(json.dumps({"project": PROJECT, "project_name": "Demo 743", "desktop_access": {"mode": "headless"},
                                    "board_url": "http://127.0.0.1:1/", "run_as_user": owner, "roles": roles}),
                        encoding="utf-8")
        return tl.load_project_config(PROJECT, path), path

    config, config_path = configured(me)
    state_path = work / "presentation.json"
    env = {**os.environ, "TERM": "xterm-256color"}
    wrapper = str(ROOT / "scripts" / tl.PANE_WINDOW_NAME)
    windows: list[list[Split]] = []

    def open_window(_layout_path, **kwargs) -> int:
        """Konsole, given the layout: one tab per slot, each the pane wrapper running the slot's attach."""
        tabs = [Split([wrapper, "--window-title", kwargs.get("window_title") or "Demo 743", "--title", f"slot {slot}",
                       *display_attach_args_for(PROJECT, slot, owner=me, gui_user=me)], env) for slot in (0, 1)]
        windows.append(tabs)
        return 0

    def showing() -> set:
        return external_presentation_clients(config, 2, runner=subprocess.run)

    def settle(want: int) -> set:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(showing()) != want:
            time.sleep(0.1)
        return showing()

    def workers() -> dict:
        return {role["role"]: tmux("display-message", "-p", "-t", f"={PROJECT}-{role['role']}:0.0",
                                   "#{session_id} #{pane_pid}") for role in roles}

    def launch() -> str:
        said = io.StringIO()
        with redirect_stdout(said):
            result = pc.launch_presentation(config, config_path=config_path, layout="separate",
                                            state_path=state_path, runner=subprocess.run, print_func=print)
        check(result == 0, f"the launch's presentation step answers 0: {result}")
        return said.getvalue()

    saved = {name: getattr(tl, name) for name in ("launch_konsole_window", "write_desktop_layout",
                                                  "presentation_pane_program_problem", "presentation_handoff_descriptor",
                                                  "_owner_process_runner")}
    try:
        tl.launch_konsole_window = open_window
        tl.write_desktop_layout = lambda *_a, **_k: ""  # the layout file is Konsole's to read; its tabs are below
        tl.presentation_pane_program_problem = lambda *_a, **_k: ""  # the wrapper is this checkout's, not root's
        tl.presentation_handoff_descriptor = lambda: (None, "")
        for role in roles:
            tmux("new-session", "-d", "-x", "120", "-y", "40", "-s", f"{PROJECT}-{role['role']}", "sleep 100000")
        pc.reconnect_display_slots(config, config_path=config_path, state_path=state_path, runner=subprocess.run)
        check(showing() == set(), "fixture: no window yet")

        said = launch()
        first = settle(2)
        check(len(windows) == 1 and len(first) == 2 and "already open" not in said,
              f"no window: the launch opens one, and its two tabs are on the slots: {len(windows)} {first} {said}")
        before = workers()

        # `sudo switchyard resume-provision` over the healthy project: the same launch, the window still open.
        said = launch()
        check(len(windows) == 1 and showing() == first,
              f"the window open: the launch opens no second one, and the same tabs show the slots: "
              f"{len(windows)} {showing()} {first}")
        check(f"switchyard: {PROJECT}'s presentation window is already open (2 terminal tabs are showing its slots); "
              "not opening another" in said, f"and says so: {said}")
        check(workers() == before, f"the workers' sessions and processes are the ones they were: {workers()} {before}")
        said = launch()
        check(len(windows) == 1 and showing() == first and "already open" in said, "run again, still one window")

        # A tenant owned by another account, from root: the slots are asked about as their owner.
        routed: list[tuple[str, tuple]] = []

        def as_owner(*, owner_user: str, runner):
            def run(args, **kwargs):
                routed.append((owner_user, tuple(args[:2])))
                return runner(args, **kwargs)
            return run

        tl._owner_process_runner = as_owner
        other, _other_path = configured("s743-owner")
        seen = external_presentation_clients(other, 2, runner=pc._tmux_runner(other, subprocess.run))
        tl._owner_process_runner = saved["_owner_process_runner"]
        check(seen == first and ("s743-owner", ("tmux", "list-clients")) in routed,
              f"another account's tenant: its slots' clients are read as its owner, and the open window is seen: "
              f"{seen} {sorted(set(routed))}")
        source = ast.parse(textwrap.dedent(inspect.getsource(pc.launch_presentation)))
        shown = [kw.value for node in ast.walk(source) if isinstance(node, ast.Call)
                 and getattr(node.func, "id", "") == "_launch_separate" for kw in node.keywords if kw.arg == "shown"]
        check(len(shown) == 1 and "runner=owner_runner" in ast.unparse(shown[0])
              and "external_presentation_clients" in ast.unparse(shown[0]),
              f"and the launch asks with that runner, the one every slot call of the launch uses: "
              f"{[ast.unparse(v) for v in shown]}")

        # The window closed: the next launch opens one, once.
        for tab in windows[0]:
            tab.close()
        check(settle(0) == set(), "fixture: the window is closed")
        said = launch()
        reopened = settle(2)
        check(len(windows) == 2 and len(reopened) == 2 and "already open" not in said,
              f"closed, the launch opens it again: {len(windows)} {reopened}")
        launch()
        check(len(windows) == 2 and showing() == reopened, "and run again, opens no other")
        check(workers() == before, "the workers are untouched throughout")

        # An explicit bootstrap is asked for a window, and gets one, as before: it calls the opener without asking.
        from scripts import presentation_window_launch as window_launch

        state = pc._read_state(state_path, config=config, config_path=config_path)
        window_launch._launch_separate(config, state, config_path=config_path, runner=subprocess.run,
                                       process_launcher=None)
        action = ast.parse(textwrap.dedent(inspect.getsource(pc.presentation_action)))
        asks = [kw.arg for node in ast.walk(action) if isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "_launch_separate" for kw in node.keywords]
        check(len(windows) == 3 and "shown" not in asks and asks,
              f"bootstrap's opener does not ask, and opens a window over the open one: {len(windows)} {asks}")
        for tab in windows[2]:
            tab.close()

        # Through the bridge the window is the caller's to open: it is still handed back, as before.
        read_end, write_end = os.pipe()
        tl.presentation_handoff_descriptor = lambda: (write_end, "")
        launch()
        os.set_blocking(read_end, False)
        try:
            handed = json.loads(os.read(read_end, 1 << 20).decode())
        except (BlockingIOError, ValueError):
            handed = {}  # nothing was handed back
        os.close(read_end)
        try:
            os.close(write_end)  # the handoff closes its end once written; a launch that wrote nothing has not
        except OSError:
            pass
        check(handed.get("project") == PROJECT and len(windows) == 3,
              f"a bridged launch hands its window back and opens none itself: {handed}")
    finally:
        for name, value in saved.items():
            setattr(tl, name, value)
        for tabs in windows:
            for tab in tabs:
                tab.close()
        if socket.exists():
            tmux("kill-server", check_rc=False)
        shutil.rmtree(work, ignore_errors=True)
    print(f"presentation_single_window_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
