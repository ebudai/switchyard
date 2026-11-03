#!/usr/bin/env python3
"""SYRD-733 on real tmux: each split's header names the role its slot shows now, through every presentation change.

Live: slot 0 was configured for Inspector, and Librarian detached. `switchyard present show librarian --slot 0` put
Librarian in the slot -- its display proxy, `status-left` and `@switchyard_role` all said so -- while the Konsole split
header went on saying `Inspector`. The pane wrapper names its split once, as the window opens, from the configured slot
role, and nothing told the split again.

Here, the configuration has that shape:
- Inspector configured in slot 0;
- Main configured in slot 1;
- the Director in slot 2;
- Librarian detached.

Every worker is a real tmux session on a private server. Each split is a real terminal: a pty running the shipped
`switchyard-pane-window` with the bootstrap title a launch gives it and the attach a launch runs. What the terminal
receives is read as Konsole reads it: OSC 30 is the split's header, OSC 0 and OSC 2 the window's title. The changes are
made by the product's own verbs:
- show, swap, hide and restore;
- the worker's session ending, and recovery once it is back;
- every display slot respawned;
- the window closed and opened again.

After each change, each split's header is the label of the role its slot shows, "Slot N: hidden", or the role's label
saying it is disconnected. The window's title is the project's, always. The workers are never restarted or typed into,
the configured slots are unchanged, and the slots' tmux labels agree with the headers.

Main runs first, from a git archive of its own scripts and tests: its header stays `Inspector` after the swap. Each run
uses this host's tmux, and every build named in SWITCHYARD_TEST_TMUX_BINARIES (colon-separated), as a separate run.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "24525a9337206e495eca91eb7f34e6692f076bbd"
PROJECT = "demo"
WINDOW = "Demo Team"
LABELS = {"inspector": "Inspector", "main": "Main Developer", "director": "Director", "librarian": "Librarian"}
OSC = re.compile(rb"\x1b\](\d+);([^\x07\x1b]*)(?:\x07|\x1b\\)")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Split:
    """One Konsole split, as its terminal sees it: a pty running the pane wrapper, every escape kept in order."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.data = b""
        self.lock = threading.Lock()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.stopped = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        while not self.stopped.is_set():
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
                if ready:
                    chunk = os.read(self.fd, 65536)
                    if not chunk:
                        return
                    with self.lock:
                        self.data += chunk
            except OSError:
                return

    def escapes(self, code: str) -> list[str]:
        with self.lock:
            data = self.data
        return [value.decode("utf-8", "replace") for number, value in OSC.findall(data) if number.decode() == code]

    def header(self) -> str:
        """What Konsole shows in this split's header: the last OSC 30 it was sent."""
        seen = self.escapes("30")
        return seen[-1] if seen else ""

    def windows(self) -> set[str]:
        return set(self.escapes("0")) | set(self.escapes("2"))

    def close(self) -> None:
        # The reader first: once the descriptor is closed its number is reused, and a reader still polling it would
        # take whatever the next file opened with that number was sent.
        if self.stopped.is_set():
            return  # closed already: its descriptor's number may be someone else's by now
        self.stopped.set()
        self.reader.join()
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def scenario(root: Path, tmux_bin: str) -> dict:
    for extra in (str(root), str(root / "scripts"), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from scripts import presentation_controller as pc
    from scripts import team_launcher as tl
    from scripts.presentation_window_launch import display_attach_args_for

    work = Path(tempfile.mkdtemp(prefix="s733-", dir="/tmp"))
    bindir = work / "bin"
    bindir.mkdir()
    (bindir / "tmux").symlink_to(tmux_bin)
    os.environ["PATH"] = f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    os.environ["TMUX_TMPDIR"] = str(work)
    socket = work / f"tmux-{os.getuid()}" / "default"
    socket.parent.mkdir(mode=0o700)  # the directory tmux makes for itself under TMUX_TMPDIR, made here for `-S`
    probe = subprocess.run([tmux_bin, "-S", str(socket), "list-sessions"], capture_output=True, text=True)
    assert probe.returncode != 0, f"refusing to run: a server already answers on {socket}"

    def tmux(*args: str, check_rc: bool = True) -> str:
        proc = subprocess.run([tmux_bin, "-S", str(socket), *args], capture_output=True, text=True)
        if check_rc and proc.returncode != 0:
            raise AssertionError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    roles = [{"role": "inspector", "slot": 0, "presentation_label": "Inspector"},
             {"role": "main", "slot": 1, "presentation_label": "Main Developer"},
             {"role": "director", "slot": 2},
             {"role": "librarian", "detached": True, "presentation_label": "Librarian"}]
    for role in roles:
        role.update({"cli": "claude", "workdir": str(work / "work" / role["role"])})
        Path(role["workdir"]).mkdir(parents=True)
    config_path = work / f"{PROJECT}.json"
    config_text = json.dumps({"project": PROJECT, "project_name": WINDOW, "desktop_access": {"mode": "headless"},
                              "board_url": "http://127.0.0.1:1/", "run_as_user": tl.current_user_name(),
                              "roles": roles})
    config_path.write_text(config_text, encoding="utf-8")
    config = tl.load_project_config(PROJECT, config_path)
    state_path = work / "presentation.json"
    director = {"TICKET_BOARD_CALLER_ROLE": "director"}
    version = subprocess.run([tmux_bin, "-V"], capture_output=True, text=True).stdout.strip()
    out: dict = {"tmux": version, "steps": []}
    splits: dict[int, Split] = {}
    env = {**os.environ, "TERM": "xterm-256color"}
    wrapper = str(root / "scripts" / tl.PANE_WINDOW_NAME)

    def open_split(slot: int, title: str) -> None:
        # What a launch gives the split: the wrapper, its two titles, and the slot's attach.
        argv = [wrapper, "--window-title", tl.project_window_title(config), "--title", title,
                *display_attach_args_for(PROJECT, slot, owner=tl.current_user_name(),
                                            gui_user=tl.current_user_name())]
        splits[slot] = Split(argv, env)

    def workers() -> dict:
        return {name: tmux("display-message", "-p", "-t", f"={PROJECT}-{name}:0.0", "#{pane_pid}", check_rc=False)
                for name in LABELS}

    def typed() -> dict:
        return {name: tmux("capture-pane", "-p", "-t", f"={PROJECT}-{name}:0.0", check_rc=False).strip()
                for name in LABELS}

    def read(step: str, want: dict[int, str]) -> None:
        """Each split's header once it settles on what is wanted, or what it last said."""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and any(splits[s].header() != w for s, w in want.items()):
            time.sleep(0.05)
        time.sleep(0.3)  # anything sent after it settled
        out["steps"].append({
            "step": step,
            "headers": {str(slot): split.header() for slot, split in splits.items()},
            "roles": {str(slot): tmux("display-message", "-p", "-t", f"={PROJECT}-display-{slot}:0.0",
                                      "#{@switchyard_role}", check_rc=False) for slot in splits},
            "workers": workers(),
            "typed": typed(),
        })

    def act(action: str, **kwargs) -> None:
        pc.presentation_action(config, config_path=config_path, action=action, state_path=state_path,
                               environ=director, runner=subprocess.run, **kwargs)

    try:
        for name in LABELS:
            tmux("new-session", "-d", "-x", "120", "-y", "40", "-s", f"{PROJECT}-{name}", "sleep 100000")
        # The slots as a launch builds them, from the presentation's mapping; then the window.
        pc.reconnect_display_slots(config, config_path=config_path, state_path=state_path, runner=subprocess.run)
        configured = tl.presentation_slot_titles(config, 2)
        out["bootstrap"] = configured
        for slot in (0, 1):
            open_split(slot, configured[slot])
        read("opened", {0: LABELS["inspector"], 1: LABELS["main"]})
        act("show", role_name="librarian", slot=0)
        read("show librarian 0", {0: LABELS["librarian"]})
        act("swap", slot=0, other_slot=1)
        read("swap 0 1", {0: LABELS["main"], 1: LABELS["librarian"]})
        act("hide", slot=0)
        read("hide 0", {0: "Slot 0: hidden"})
        act("show", role_name="librarian", slot=0)
        read("show librarian 0 again", {0: LABELS["librarian"], 1: "Slot 1: hidden"})
        act("restore", layout="default")
        read("restore default", {0: LABELS["inspector"], 1: LABELS["main"]})
        act("show", role_name="librarian", slot=0)
        read("show librarian 0, third", {0: LABELS["librarian"]})
        # The worker's session ends: the slot's own hook parks it, and says so in the header.
        tmux("kill-session", "-t", f"={PROJECT}-librarian")
        read("librarian's session ended", {0: f"{LABELS['librarian']} (disconnected)"})
        tmux("new-session", "-d", "-x", "120", "-y", "40", "-s", f"{PROJECT}-librarian", "sleep 100000")
        pc.reconnect_role_slots(config, config_path=config_path, role_name="librarian", state_path=state_path,
                                runner=subprocess.run)
        read("librarian back, its slot reconnected", {0: LABELS["librarian"]})
        pc.reconnect_display_slots(config, config_path=config_path, state_path=state_path, runner=subprocess.run)
        read("every display slot respawned", {0: LABELS["librarian"]})
        # The slots rebuilt while the worker is down: the header says so, then follows it back.
        tmux("kill-session", "-t", f"={PROJECT}-librarian")
        read("librarian's session ended again", {0: f"{LABELS['librarian']} (disconnected)"})
        pc.reconnect_display_slots(config, config_path=config_path, state_path=state_path, runner=subprocess.run)
        read("slots respawned with librarian down", {0: f"{LABELS['librarian']} (not running)"})
        tmux("new-session", "-d", "-x", "120", "-y", "40", "-s", f"{PROJECT}-librarian", "sleep 100000")
        pc.reconnect_role_slots(config, config_path=config_path, role_name="librarian", state_path=state_path,
                                runner=subprocess.run)
        read("librarian back again", {0: LABELS["librarian"]})
        # The window closed and opened again, with the titles a launch now gives it.
        for split in splits.values():
            split.close()
        state = pc._read_state(state_path, config=config, config_path=config_path)
        out["reopened bootstrap"] = window_open_titles(config, config_path, state)
        # Opened with the configured titles, as a window from an older owner half or a bridged launch still is: the
        # slot's client corrects the header as it attaches.
        for slot in (0, 1):
            open_split(slot, configured[slot])
        read("window reopened", {0: LABELS["librarian"], 1: LABELS["main"]})
        out["reopened first header"] = (splits[0].escapes("30") or [""])[0]
        out["windows"] = sorted(set().union(*(split.windows() for split in splits.values())))
        out["config unchanged"] = config_path.read_text(encoding="utf-8") == config_text
        out["configured slots"] = {role.role: role.slot for role in tl.load_project_config(PROJECT, config_path).roles}
    finally:
        for split in splits.values():
            split.close()
        if socket.exists():
            tmux("kill-server", check_rc=False)
        shutil.rmtree(work, ignore_errors=True)
    return out


def window_open_titles(config, config_path: Path, state: dict) -> dict:
    """The titles a window opened now gives its splits, from the launch itself, both ways it opens one.

    Handed to the desktop account through the bridge: the payload the owner half writes. Opened here: the layout it
    writes. Only the descriptor, the layout write and the pane program's check stand in.
    """
    from scripts import presentation_window_launch as launch

    tl = launch.team_launcher
    saved = (tl.presentation_handoff_descriptor, tl.write_desktop_layout, tl.presentation_pane_program_problem)
    seen: dict = {}
    read_end, write_end = os.pipe()
    try:
        tl.presentation_handoff_descriptor = lambda: (write_end, "")
        launch._launch_separate(config, state, config_path=config_path, runner=subprocess.run, process_launcher=None)
        seen["handoff"] = json.loads(os.read(read_end, 1 << 20).decode())["slot_titles"]

        def written(_path, layout, **_kwargs):
            seen["layout"] = [leaf["Title"] for leaf in tl._layout_leaves(layout)]
            return "stopped here: the layout is what was wanted"

        tl.presentation_handoff_descriptor = lambda: (None, "")
        tl.write_desktop_layout = written
        tl.presentation_pane_program_problem = lambda *_a, **_k: ""
        try:
            launch._launch_separate(config, state, config_path=config_path, output_path=Path("/nonexistent"),
                                    runner=subprocess.run, process_launcher=None)
        except SystemExit:
            pass
    finally:
        tl.presentation_handoff_descriptor, tl.write_desktop_layout, tl.presentation_pane_program_problem = saved
        os.close(read_end)
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path, tmux_bin: str) -> dict:
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), tmux_bin],
                           capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def step(got: dict, name: str) -> dict:
    return next((item for item in got["steps"] if item["step"] == name), {})


def before(got: dict) -> None:
    check(step(got, "opened").get("headers") == {"0": "Inspector", "1": "Main Developer"},
          f"main ({got['tmux']}): the window opens with the configured headers: {step(got, 'opened')}")
    shown = step(got, "show librarian 0")
    check(shown.get("roles", {}).get("0") == "librarian" and shown.get("headers", {}).get("0") == "Inspector",
          f"main ({got['tmux']}): slot 0 shows Librarian and its header still says Inspector: {shown}")
    check(got["reopened bootstrap"]["handoff"][0] == "Inspector" == got["reopened bootstrap"]["layout"][0],
          f"main ({got['tmux']}): and a window opened then names slot 0 Inspector: {got['reopened bootstrap']}")


def candidate(got: dict) -> None:
    v = got["tmux"]
    expected = {
        "opened": ({"0": "Inspector", "1": "Main Developer"}, {"0": "inspector", "1": "main"}),
        "show librarian 0": ({"0": "Librarian", "1": "Main Developer"}, {"0": "librarian", "1": "main"}),
        "swap 0 1": ({"0": "Main Developer", "1": "Librarian"}, {"0": "main", "1": "librarian"}),
        "hide 0": ({"0": "Slot 0: hidden", "1": "Librarian"}, {"0": "hidden", "1": "librarian"}),
        "show librarian 0 again": ({"0": "Librarian", "1": "Slot 1: hidden"}, {"0": "librarian", "1": "hidden"}),
        "restore default": ({"0": "Inspector", "1": "Main Developer"}, {"0": "inspector", "1": "main"}),
        "show librarian 0, third": ({"0": "Librarian", "1": "Main Developer"}, {"0": "librarian", "1": "main"}),
        "librarian's session ended": ({"0": "Librarian (disconnected)", "1": "Main Developer"},
                                      {"0": "librarian", "1": "main"}),
        "librarian back, its slot reconnected": ({"0": "Librarian", "1": "Main Developer"},
                                                 {"0": "librarian", "1": "main"}),
        "every display slot respawned": ({"0": "Librarian", "1": "Main Developer"}, {"0": "librarian", "1": "main"}),
        "librarian's session ended again": ({"0": "Librarian (disconnected)", "1": "Main Developer"},
                                            {"0": "librarian", "1": "main"}),
        "slots respawned with librarian down": ({"0": "Librarian (not running)", "1": "Main Developer"},
                                                {"0": "librarian", "1": "main"}),
        "librarian back again": ({"0": "Librarian", "1": "Main Developer"}, {"0": "librarian", "1": "main"}),
        "window reopened": ({"0": "Librarian", "1": "Main Developer"}, {"0": "librarian", "1": "main"}),
    }
    check([item["step"] for item in got["steps"]] == list(expected), f"{v}: every step ran: {got['steps']}")
    for name, (headers, roles) in expected.items():
        seen = step(got, name)
        check(seen.get("headers") == headers, f"{v}, {name}: each split's header is its slot's role now: {seen}")
        check(seen.get("roles") == roles, f"{v}, {name}: and the slot's tmux label agrees: {seen}")
    check(got["windows"] == [WINDOW], f"{v}: the window's title is the project's, whatever the slots show: "
          f"{got['windows']}")
    check(got["reopened first header"] == "Inspector",
          f"{v}: the reopened split started out stale, so the attach is what corrected it: {got['reopened first header']}")
    check(got["reopened bootstrap"] == {"handoff": ["Librarian", "Main Developer", "Director"],
                                        "layout": ["Librarian", "Main Developer", "Director"]},
          f"{v}: a window opened after the swap starts with the roles it shows, handed off or opened here: "
          f"{got['reopened bootstrap']}")
    # The worker behind Inspector, Main and the Director: one process each, from first to last, and nothing typed.
    for name in ("inspector", "main", "director"):
        pids = {item["workers"][name] for item in got["steps"]}
        check(len(pids) == 1 and "" not in pids, f"{v}: {name}'s worker is never restarted: {pids}")
    first = step(got, "opened")["workers"]["librarian"]
    check(first and first == step(got, "show librarian 0, third")["workers"]["librarian"],
          f"{v}: librarian's worker runs on through every relabel until its own session ends")
    check(all(not text for item in got["steps"] for text in item["typed"].values()),
          f"{v}: nothing is typed into any worker: {[item['typed'] for item in got['steps']]}")
    check(got["config unchanged"] and got["configured slots"] == {"inspector": 0, "main": 1, "director": 2,
                                                                   "librarian": None},
          f"{v}: the configured slots are as they were: {got['configured slots']}")


def binaries() -> list[str]:
    host = shutil.which("tmux")
    assert host, "presentation_split_title_test: tmux is required"
    extra = [path for path in os.environ.get("SWITCHYARD_TEST_TMUX_BINARIES", "").split(":") if path]
    return [str(Path(host).resolve()), *extra]


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3])))
        return 0
    for tmux_bin in binaries():
        if os.environ.get("SYRD733_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
            with tempfile.TemporaryDirectory(prefix="syrd733-before.", dir="/tmp") as tmp:
                before(run(tree_at(BEFORE, Path(tmp) / "before"), tmux_bin))
        candidate(run(ROOT, tmux_bin))
    print(f"presentation_split_title_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
