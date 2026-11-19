#!/usr/bin/env python3
"""SYRD-834: Ctrl-V in a presentation tab hands the clipboard's image to the role on screen there.

Since SYRD-791 a role pane cannot read the desktop owner's clipboard, so Ctrl-V with an image copied failed in every
role pane. `switchyard paste-image` hands one image over when the owner runs it. Each tab's program,
`switchyard-pane-window`, runs as that owner, so it now relays the tab: Ctrl-V with an image copied runs the same
handover and pastes the image's path into the pane; everything else passes through.

The tab here is real: a pty for the terminal, running the shipped `switchyard-pane-window` with the arguments the
product's own layout gives a tab, and the shipped relay beside it. The attach is a stand-in program in a pty of its own,
which sets raw mode, asks for bracketed paste and a header as tmux does, and reports every byte it receives. The
handover itself (`paste_image.pane_image`) is stood in beside the shipped relay, so its answer is chosen per step and
every call is recorded. Main runs first, from a git archive: with an image copied, its tab hands the role a bare Ctrl-V.

Then, on this tree:
- every byte value but 0x16 passes through unchanged, typed or in a paste, a 0x16 in a paste included, and promptly;
- Ctrl-V with an image: the pane gets the image's path, as a bracketed paste while the attach asked for one and as
  plain text when it did not; keys typed during the handover reach the pane after it;
- Ctrl-V with text or nothing: the pane gets the 0x16;
- a refusal, a crash, or a clipboard that never answers: the split's header says why, then shows its own header again,
  and the pane gets nothing for that Ctrl-V;
- a project with no approved desktop policy: no relay at all, the attach runs in its place;
- the window size follows the terminal's;
- the attach exits: the tab says so, the terminal is handed back as it was, and Ctrl-V or anything else reaches nothing.

`pane_image` and `pane_handoff_offered` themselves are checked with the clipboard and the grant stood in, as
image_handoff_test checks `deposit`.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import pty
import pwd
import re
import select
import shlex
import shutil
import signal
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
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from standalone_test_runner import run_module_tests  # noqa: E402
from scripts import desktop_access, paste_image  # noqa: E402
from scripts import switchyard_pane_relay as relay  # noqa: E402

#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "878d8bfbe9d851791aaeef697eeacf5be0d4debc"
ME = pwd.getpwuid(os.geteuid())
PROJECT = "demo"
IMAGE = f"/run/switchyard-desktop/{ME.pw_uid}/switchyard-paste/{PROJECT}/paste-{'ab' * 16}.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 300
CHECKS = 0

ATTACH = r'''
import fcntl, os, signal, struct, sys, termios, tty
log = sys.argv[1]
tty.setraw(0)
def say(data):
    os.write(1, data)
with open(log + ".pid", "w") as handle:
    handle.write(f"{os.getpid()} {os.getppid()}")
# The slot shows another role than the one the window opened with, as after `present show` (SYRD-733).
say(b"\x1b[?2004h\x1b]0;Demo Team\x07\x1b]30;Librarian\x07")
def winch(*_):
    rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))[:2]
    with open(log + ".winch", "a") as handle:
        handle.write(f"{rows}x{cols}\n")
winch()
signal.signal(signal.SIGWINCH, winch)
def end(*_):
    say(b"last words\r\n")
    if os.path.exists(log + ".hold") and os.fork() == 0:
        with open(log + ".held", "w") as handle:
            handle.write(str(os.getpid()))
        os.execvp("sleep", ["sleep", "60"])  # something the attach started, still holding its terminal
    os._exit(3)
signal.signal(signal.SIGUSR1, end)
signal.signal(signal.SIGUSR2, lambda *_: say(b"\x1b[?2004l"))
while True:
    data = os.read(0, 65536)
    if not data:
        break
    with open(log, "ab") as handle:
        handle.write(data)
    say(b"IN:" + data.hex().encode() + b"\r\n")
'''

STANDIN = r'''
"""Stand-in for paste_image in a test tree: answers as standin-control.json says and records every call."""
import json, os, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
class PasteImageError(RuntimeError):
    pass
def _control():
    return json.loads((HERE / "standin-control.json").read_text())
def _record(entry):
    with open(HERE / "standin-calls.jsonl", "a") as handle:
        handle.write(json.dumps(entry) + "\n")
def pane_handoff_offered(project):
    _record({"call": "offered", "project": project})
    return bool(_control().get("offered"))
def pane_image(project):
    control = _control()
    _record({"call": "image", "project": project, "uid": os.geteuid()})
    time.sleep(control.get("delay", 0))
    answer = control["answer"]
    if answer == "image":
        return Path(control["path"])
    if answer == "none":
        return None
    if answer == "hang":
        time.sleep(3600)
    if answer == "crash":
        raise KeyError("standin-crash")
    raise PasteImageError(control["error"])
'''


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# --- a tab ------------------------------------------------------------------------------------------------------------


class Tree:
    """A release's scripts directory with this tree's (or main's) pane window and relay, and the handover stood in."""

    def __init__(self, root: Path, source: Path) -> None:
        self.scripts = root / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("switchyard-pane-window", "switchyard_pane_relay.py"):
            if (source / name).exists():
                shutil.copy2(source / name, self.scripts / name)
        (self.scripts / "__init__.py").write_text("")
        (self.scripts / "paste_image.py").write_text(STANDIN)
        (self.scripts / "desktop_access.py").write_text("class DesktopAccessError(Exception):\n    pass\n")
        self.calls_path = self.scripts / "standin-calls.jsonl"
        self.answer(offered=True, answer="none")

    def answer(self, **control) -> None:
        (self.scripts / "standin-control.json").write_text(json.dumps(control))

    def calls(self, kind: str = "image") -> list[dict]:
        if not self.calls_path.exists():
            return []
        entries = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
        return [entry for entry in entries if entry["call"] == kind]


def tab_arguments(source_root: Path, program: Path) -> list[str]:
    """The pane window's own arguments in slot 0 of the layout `source_root`'s product writes for a crossing window."""
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from scripts import presentation_controller as pc\n"
        "layout = pc.presentation_layout_payload(sys.argv[1], slot_count=2, owner='tenant-owner', gui_user=sys.argv[2],"
        " pane_program=Path(sys.argv[3]), slot_titles=['Main Developer', 'Director'], window_title='Demo Team')\n"
        "from scripts import team_launcher as tl\n"
        "print(json.dumps(tl._layout_leaves(layout)[0]['Command']))\n"
    )
    proc = subprocess.run([sys.executable, "-c", script, PROJECT, ME.pw_name, str(program)], cwd=source_root,
                          capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    words = shlex.split(json.loads(proc.stdout))
    assert words[0] == str(program), words
    attach = words.index("sudo") if "sudo" in words else words.index(os.environ.get("SWITCHYARD_SUDO_BIN", "sudo"))
    return words[1:attach]


class Tab:
    """One Konsole split: a pty running the pane window, whose attach is the recording stand-in."""

    def __init__(self, tree: Tree, arguments: list[str], work: Path, *, rows: int = 30, cols: int = 100,
                 deaf: bool = False) -> None:
        self.log = work / f"attach-{time.monotonic_ns()}.log"
        self.log.write_bytes(b"")
        attach = work / "attach.py"
        attach.write_text(ATTACH)
        argv = [str(tree.scripts / "switchyard-pane-window"), *arguments, sys.executable, str(attach), str(self.log)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work), "LANG": "C.UTF-8",
               "TERM": "xterm-256color", "PYTHONDONTWRITEBYTECODE": "1"}
        self.output = b""
        self.lock = threading.Lock()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - the terminal's program
            if deaf:  # blocked signals survive exec: nothing in the tab hears a hangup as a signal
                signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP})
            os.execve(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.stopped = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        wait_for(lambda: Path(str(self.log) + ".pid").exists() and b"Librarian" in self.output, 15,
                 "the attach to start")

    def _read(self) -> None:
        # Polled, so that closing the terminal really closes it: a read blocked on the descriptor would hold it open.
        while not self.stopped.is_set():
            if not select.select([self.fd], [], [], 0.05)[0]:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.output += data

    def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view[:1024]):]

    def received(self) -> bytes:
        return self.log.read_bytes()

    def attach_pids(self) -> tuple[int, int]:
        pid, parent = Path(str(self.log) + ".pid").read_text().split()
        return int(pid), int(parent)

    def headers(self) -> list[str]:
        with self.lock:
            return [m.decode() for m in re.findall(rb"\x1b\]30;([^\x07\x1b]*)\x07", self.output)]

    def hang_up(self) -> None:
        """What Konsole does as the tab closes: the terminal's master side goes."""
        self.stopped.set()
        self.reader.join()
        os.close(self.fd)
        self.fd = -1

    def close(self) -> None:
        self.stopped.set()
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.pid), sig)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        if self.fd >= 0:
            self.reader.join()
            os.close(self.fd)


def received_after(tab: Tab, before: int, expected: bytes, timeout: float = 10) -> bytes:
    return wait_for(lambda: tab.received()[before:] if len(tab.received()) - before >= len(expected) else b"",
                    timeout, f"the pane to receive {expected[:60]!r}")


# --- main first -------------------------------------------------------------------------------------------------------


def main_tree(work: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE, "scripts"], capture_output=True, check=True)
    root = work / "main-source"
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(root, filter="data")
    return root


def test_main_hands_the_role_a_bare_ctrl_v() -> None:
    with tempfile.TemporaryDirectory(prefix="p834m.") as tmp:
        work = Path(tmp)
        source = main_tree(work)
        tree = Tree(work / "release", source / "scripts")
        arguments = tab_arguments(source, tree.scripts / "switchyard-pane-window")
        check("--paste-project" not in arguments, arguments)
        tab = Tab(tree, arguments, work)
        try:
            tree.answer(offered=True, answer="image", path=IMAGE)
            tab.send(b"\x16")
            check(received_after(tab, 0, b"\x16") == b"\x16", tab.received())
            time.sleep(1.0)
            check(tab.received() == b"\x16" and tree.calls() == [], (tab.received(), tree.calls()))
        finally:
            tab.close()
    print("main: with an image copied, Ctrl-V in a tab hands the role a bare 0x16 and nothing reads the clipboard")


# --- this tree --------------------------------------------------------------------------------------------------------


def candidate_tab(work: Path) -> tuple[Tree, Tab, list[str]]:
    tree = Tree(work / "release", ROOT / "scripts")
    arguments = tab_arguments(ROOT, tree.scripts / "switchyard-pane-window")
    return tree, Tab(tree, arguments, work), arguments


def test_the_layout_names_the_tabs_project_to_a_pane_window_that_relays() -> None:
    with tempfile.TemporaryDirectory(prefix="p834a.") as tmp:
        tree = Tree(Path(tmp) / "release", ROOT / "scripts")
        arguments = tab_arguments(ROOT, tree.scripts / "switchyard-pane-window")
    check(arguments == ["--window-title", "Demo Team", "--title", "Main Developer", "--paste-project", PROJECT],
          arguments)


def test_a_tenant_on_an_older_release_still_gets_working_tabs() -> None:
    """The desktop half builds the layout with its own release and runs the tenant's pane window. A tenant pinned to a
    release from before the relay has main's pane window, which would run `--paste-project` as the command."""
    with tempfile.TemporaryDirectory(prefix="p834v.") as tmp:
        work = Path(tmp)
        tree = Tree(work / "release", main_tree(work) / "scripts")
        arguments = tab_arguments(ROOT, tree.scripts / "switchyard-pane-window")
        check("--paste-project" not in arguments, arguments)
        tree.answer(offered=True, answer="image", path=IMAGE)
        tab = Tab(tree, arguments, work)
        try:
            tab.send(b"\x16")
            check(received_after(tab, 0, b"\x16") == b"\x16" and tree.calls() == [], tab.received())
        finally:
            tab.close()


def test_a_tab_passes_everything_through_and_hands_over_an_image_on_ctrl_v() -> None:
    with tempfile.TemporaryDirectory(prefix="p834c.") as tmp:
        work = Path(tmp)
        tree, tab, _arguments = candidate_tab(work)
        try:
            pid, parent = tab.attach_pids()
            check(parent != tab.pid, "the attach runs under the relay, not in its place")
            winch = Path(str(tab.log) + ".winch")
            check(winch.read_text().splitlines()[0] == "30x100", "the attach starts at the terminal's size")

            # Every byte value but Ctrl-V, typed, then a paste carrying a 0x16 of its own.
            typed = bytes(value for value in range(256) if value != 0x16)
            pasted = b"\x1b[200~text with \x16 in it\x1b[201~"
            tab.send(typed + pasted)
            check(received_after(tab, 0, typed + pasted) == typed + pasted, tab.received()[-80:])
            check(tree.calls() == [], tree.calls())
            start = time.monotonic()
            mark = len(tab.received())
            tab.send(b"z")
            received_after(tab, mark, b"z")
            check(time.monotonic() - start < 0.25, f"a keystroke took {time.monotonic() - start:.3f}s")

            # Ctrl-V with an image: its path, bracketed; what was typed during the handover comes after it.
            tree.answer(offered=True, answer="image", path=IMAGE, delay=0.6)
            mark = len(tab.received())
            tab.send(b"\x1b[A\x16")
            time.sleep(0.1)
            tab.send(b"after")
            expected = b"\x1b[A" + b"\x1b[200~" + IMAGE.encode() + b"\x1b[201~" + b"after"
            check(received_after(tab, mark, expected) == expected, tab.received()[mark:])
            check(tree.calls() == [{"call": "image", "project": PROJECT, "uid": ME.pw_uid}], tree.calls())

            # The attach turned bracketed paste off: the same path, as text.
            os.kill(pid, signal.SIGUSR2)
            wait_for(lambda: b"\x1b[?2004l" in tab.output, 5, "bracketed paste off")
            tree.answer(offered=True, answer="image", path=IMAGE)
            mark = len(tab.received())
            tab.send(b"\x16")
            check(received_after(tab, mark, IMAGE.encode()) == IMAGE.encode(), tab.received()[mark:])

            # Text or nothing on the clipboard: the CLI gets its Ctrl-V.
            tree.answer(offered=True, answer="none")
            mark = len(tab.received())
            tab.send(b"\x16x")
            check(received_after(tab, mark, b"\x16x") == b"\x16x", tab.received()[mark:])

            # A refusal: said in the header, the header restored, nothing in the pane for it.
            tree.answer(offered=True, answer="error", error="switchyard: you have not approved a desktop grant for "
                        "demo, so there is no role you may hand an image to. Your approved projects: other.")
            mark, shown = len(tab.received()), len(tab.headers())
            tab.send(b"\x16")
            time.sleep(0.2)
            tab.send(b"y")
            check(received_after(tab, mark, b"y") == b"y", tab.received()[mark:])
            said = wait_for(lambda: tab.headers()[shown:], 5, "the refusal in the header")
            check(said[0] == "Ctrl-V: you have not approved a desktop grant for demo, so there is no role you may "
                  "hand an image to", said)
            back = wait_for(lambda: tab.headers()[shown + 1:], relay.MESSAGE_SECONDS + 5, "the header restored")
            check(back[0] == "Librarian", back)

            # A crash in the handover is a refusal too.
            tree.answer(offered=True, answer="crash")
            mark, shown = len(tab.received()), len(tab.headers())
            tab.send(b"\x16w")
            check(received_after(tab, mark, b"w") == b"w", tab.received()[mark:])
            said = wait_for(lambda: tab.headers()[shown:], 5, "the crash in the header")
            check(said[0].startswith("Ctrl-V: the handover failed: KeyError"), said)

            # A clipboard that never answers is ended, and what was typed meanwhile still arrives.
            tree.answer(offered=True, answer="hang")
            mark, shown = len(tab.received()), len(tab.headers())
            tab.send(b"\x16")
            time.sleep(0.3)
            tab.send(b"v")
            time.sleep(1.0)
            check(tab.received()[mark:] == b"", "input waits behind the handover")
            check(received_after(tab, mark, b"v", relay.HANDOVER_SECONDS + 10) == b"v", tab.received()[mark:])
            said = wait_for(lambda: [h for h in tab.headers()[shown:] if h.startswith("Ctrl-V")], 5,
                            "the timeout in the header")
            check(said[0] == f"Ctrl-V: the clipboard did not answer within {relay.HANDOVER_SECONDS:.0f} seconds", said)

            # The window size follows the terminal's.
            fcntl.ioctl(tab.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 41, 133, 0, 0))
            wait_for(lambda: winch.exists() and "41x133" in winch.read_text(), 5, "the resize")

            # The attach ends: the tab says so, is handed back cooked, and nothing reaches anything.
            check(not termios.tcgetattr(tab.fd)[3] & termios.ICANON, "raw while the attach runs")
            os.kill(pid, signal.SIGUSR1)
            wait_for(lambda: b"exited with status 3" in tab.output, 10, "the inert notice")
            check(tab.output.index(b"last words") < tab.output.index(b"exited with status 3"), "its last output")
            wait_for(lambda: Path(f"/proc/{tab.pid}/comm").read_text().strip() == "sleep", 5, "the inert wait")
            attrs = termios.tcgetattr(tab.fd)
            check(attrs[3] & termios.ICANON and attrs[3] & termios.ECHO, "the terminal is handed back cooked")
            tree.answer(offered=True, answer="image", path=IMAGE)
            calls = len(tree.calls())
            tab.send(b"\x16ls\r")
            time.sleep(1.0)
            check(len(tree.calls()) == calls, "nothing hands over after the attach ended")
            children = subprocess.run(["ps", "-o", "pid=", "--ppid", str(tab.pid)], capture_output=True, text=True)
            check(children.stdout.strip() == "", f"no process under the inert tab: {children.stdout}")
        finally:
            tab.close()
    print("candidate: bytes pass through, Ctrl-V hands over an image or passes on, refusals are said, resize and "
          "the inert end hold")


def test_closing_the_tab_ends_the_attach() -> None:
    """However the tab goes -- its terminal hangs up, heard as a signal or only as the end of input, or the relay is
    told to end -- the attach goes with it; and the attach ending ends the relay even while something it started
    still holds its terminal open."""

    def gone(pid: int) -> bool:
        stat = Path(f"/proc/{pid}/stat")
        return not stat.exists() or stat.read_text().split()[2] == "Z"

    with tempfile.TemporaryDirectory(prefix="p834x.") as tmp:
        work = Path(tmp)
        for how in ("hang up", "hang up, unheard", "end the relay"):
            tree = Tree(work / how.replace(" ", "-").replace(",", ""), ROOT / "scripts")
            tab = Tab(tree, tab_arguments(ROOT, tree.scripts / "switchyard-pane-window"), work,
                      deaf=how == "hang up, unheard")
            pid, parent = tab.attach_pids()
            try:
                check(not gone(pid), f"{how}: running")
                if how == "end the relay":
                    os.kill(parent, signal.SIGTERM)
                else:
                    tab.hang_up()
                wait_for(lambda: gone(pid), 10, f"the attach to end ({how})")
                check(True, how)
            finally:
                tab.close()
        tree = Tree(work / "held", ROOT / "scripts")
        tab = Tab(tree, tab_arguments(ROOT, tree.scripts / "switchyard-pane-window"), work)
        try:
            Path(str(tab.log) + ".hold").write_text("")
            os.kill(tab.attach_pids()[0], signal.SIGUSR1)
            wait_for(lambda: b"exited with status 3" in tab.output, 10, "the inert notice while the terminal is held")
            check(True, "held")
        finally:
            tab.close()
            held = Path(str(tab.log) + ".held")
            try:
                os.kill(int(wait_for(lambda: held.exists() and held.read_text(), 5, "the held pid")), signal.SIGKILL)
            except (ProcessLookupError, AssertionError):
                pass

def test_a_project_without_a_desktop_policy_gets_no_relay() -> None:
    with tempfile.TemporaryDirectory(prefix="p834h.") as tmp:
        work = Path(tmp)
        tree = Tree(work / "release", ROOT / "scripts")
        tree.answer(offered=False, answer="image", path=IMAGE)
        tab = Tab(tree, tab_arguments(ROOT, tree.scripts / "switchyard-pane-window"), work)
        try:
            _pid, parent = tab.attach_pids()
            check(parent == tab.pid, "the attach runs in the relay's place, straight under the pane window")
            check(tree.calls("offered") == [{"call": "offered", "project": PROJECT}], tree.calls("offered"))
            tab.send(b"\x16")
            check(received_after(tab, 0, b"\x16") == b"\x16", tab.received())
            time.sleep(0.5)
            check(tree.calls() == [], tree.calls())
        finally:
            tab.close()
    print("headless: the attach runs in the relay's place and Ctrl-V is the CLI's")


def test_a_release_without_the_relay_still_opens_its_tabs() -> None:
    with tempfile.TemporaryDirectory(prefix="p834r.") as tmp:
        work = Path(tmp)
        tree = Tree(work / "release", ROOT / "scripts")
        arguments = tab_arguments(ROOT, tree.scripts / "switchyard-pane-window")
        check("--paste-project" in arguments, "named while the relay was there")
        (tree.scripts / "switchyard_pane_relay.py").unlink()
        tree.answer(offered=True, answer="image", path=IMAGE)
        tab = Tab(tree, arguments, work)
        try:
            check(tab.attach_pids()[1] == tab.pid, "the attach runs straight under the pane window")
            tab.send(b"\x16")
            check(received_after(tab, 0, b"\x16") == b"\x16", tab.received())
        finally:
            tab.close()


# --- the scanner and the watcher --------------------------------------------------------------------------------------


def test_only_a_typed_ctrl_v_counts() -> None:
    def ctrl_vs(*chunks: bytes) -> tuple[int, bytes]:
        scanner = relay.InputScanner()
        pieces = [piece for chunk in chunks for piece in scanner.feed(chunk)]
        return pieces.count(None), b"".join(piece for piece in pieces if piece is not None)

    check(ctrl_vs(b"a\x16b") == (1, b"ab"), "typed")
    check(ctrl_vs(b"\x1b[200~a\x16b\x1b[201~") == (0, b"\x1b[200~a\x16b\x1b[201~"), "inside a paste")
    check(ctrl_vs(b"\x1b[20", b"0~a\x16", b"b\x1b[2", b"01~\x16") == (1, b"\x1b[200~a\x16b\x1b[201~"),
          "markers split across reads")
    check(ctrl_vs(b"\x1b]11;rgb:\x16\x07\x16") == (1, b"\x1b]11;rgb:\x16\x07"), "inside a terminal's reply")
    check(ctrl_vs(b"\x1bP1$r\x16\x1b\\\x16") == (1, b"\x1bP1$r\x16\x1b\\"), "inside a DCS reply")
    check(ctrl_vs(b"\x1b[A\x16\x1b[1;5D\x16") == (2, b"\x1b[A\x1b[1;5D"), "after cursor keys")
    check(ctrl_vs(b"\x1b(B\x16") == (1, b"\x1b(B"), "after a charset designation")
    check(ctrl_vs(b"\x1b[1\x1b[200~a\x16b\x1b[201~") == (0, b"\x1b[1\x1b[200~a\x16b\x1b[201~"),
          "a paste right after an unfinished CSI")


def test_the_watcher_reads_what_the_attach_asked_for() -> None:
    watcher = relay.OutputWatcher("start")
    watcher.feed(b"\x1b[?2004h")
    check(watcher.bracketed, "on")
    watcher.feed(b"\x1b[?1000;2004l")
    check(not watcher.bracketed, "off within a list")
    watcher.feed(b"\x1b]0;Demo\x07\x1b]30;Libr")
    check(not watcher.between_sequences and watcher.header == "start", "mid-OSC")
    watcher.feed(b"arian\x07")
    check(watcher.between_sequences and watcher.header == "Librarian", watcher.header)
    watcher.feed(b"\x1b]30;Inspector\x1b\\")
    check(watcher.header == "Inspector", "ST-terminated")
    watcher.feed(b"\x1b[3\x1b]30;Director\x07")
    check(watcher.header == "Director", "a header right after an unfinished CSI")
    for partial in (b"\x1b", b"\x1b[3", b"\xc3", b"\xe2\x82", b"\xf0\x9f\x98"):
        fresh = relay.OutputWatcher()
        fresh.feed(b"text" + partial)
        check(not fresh.between_sequences, partial)
    for whole in (b"\x1b[31m", "é".encode(), "€".encode(), "😀".encode(), b"\x1b(B"):
        fresh = relay.OutputWatcher()
        fresh.feed(b"text" + whole)
        check(fresh.between_sequences, whole)


def test_the_relay_writes_its_own_bytes_only_between_the_attachs_sequences() -> None:
    tab = relay.Relay(["true"], PROJECT, "Main Developer")
    written: list[bytes] = []
    tab._write_terminal = written.append
    tab._from_attach(b"\x1b]30;Libr")
    tab._show("switchyard: no. Really.")
    check(written == [b"\x1b]30;Libr"], written)
    tab._from_attach(b"arian\x07\x1b[3")
    check(written[-1] == b"arian\x07\x1b[3", "still inside a CSI")
    tab._from_attach(b"1m")
    check(written[-2:] == [b"1m", b"\x1b]30;Ctrl-V: no\x07"], written)
    tab._say(b"now")
    check(written[-1] == b"now", "at a boundary it is written at once")


def test_a_refusal_is_one_short_plain_line() -> None:
    check(relay.brief("switchyard: that image is larger than the 10 MiB limit for a handoff; nothing was written.")
          == "Ctrl-V: that image is larger than the 10 MiB limit for a handoff", "first clause")
    long = relay.brief("switchyard: " + "x" * 300)
    check(len(long) == relay.MESSAGE_MAX and long.endswith("…"), long)
    check(relay.brief("a\x1b]30;evil\x07b.") == "Ctrl-V: a ]30;evil b", "control characters")


# --- the handover -----------------------------------------------------------------------------------------------------


class Board:
    def __init__(self, types: list[str], data: bytes = PNG) -> None:
        self.offered, self.data, self.reads = types, data, []
        self.reader = ""

    def types(self) -> list[str]:
        return list(self.offered)

    def read(self, mime: str, *, max_bytes: int) -> bytes:
        self.reads.append(mime)
        return self.data


def _install_policy(home: Path, *, tenant_uid: int = 0, project: str = PROJECT) -> None:
    policy = desktop_access.generated_policy(project=project, tenant=pwd.getpwuid(tenant_uid).pw_name,
                                             gui_user=ME.pw_name, approved_by=ME.pw_name,
                                             approved_at="2026-11-19T00:00:00Z")
    path = home / ".config/switchyard/desktop-access" / f"switchyard-desktop-{ME.pw_uid}-{tenant_uid}-{project}.json"
    desktop_access.atomic_json(path, policy)


def test_the_handover_writes_one_image_and_no_manifest() -> None:
    with tempfile.TemporaryDirectory(prefix="p834i.") as tmp:
        home, grants = Path(tmp) / "home", Path(tmp) / "grants"
        grants.mkdir()
        _install_policy(home)
        granted: list[tuple[str, int, str]] = []
        kwargs = dict(uid=ME.pw_uid, home=home, grant_dir_for=lambda _me: grants,
                      grant=lambda path, uid, perms: granted.append((Path(path).name, uid, perms)),
                      token=lambda: "cd" * 16)
        leaf = grants / "switchyard-paste" / PROJECT
        leaf.mkdir(parents=True)
        leaf.chmod(0o700)  # as prepare_handoff_dir leaves it: the sweep touches nothing in any other directory
        stale = leaf / f"paste-{'ee' * 16}.png"
        stale.write_bytes(PNG)
        os.utime(stale, (time.time() - desktop_access.HANDOFF_TTL_SECONDS - 5,) * 2)
        with _prepared(grants):
            image = _outcome(paste_image.pane_image, PROJECT,
                             clipboard=Board(["text/plain", "image/jpeg", "image/png"]), **kwargs)
        check(image == leaf / f"paste-{'cd' * 16}.png", image)
        check(image.read_bytes() == PNG and image.stat().st_mode & 0o777 == 0o600, oct(image.stat().st_mode))
        check(sorted(p.name for p in leaf.iterdir()) == [image.name], "one image, no manifest, the expired one gone")
        check(granted == [(image.name, 0, "r--")], granted)

        text = Board(["text/plain", "UTF8_STRING"])
        with _prepared(grants):
            check(_outcome(paste_image.pane_image, PROJECT, clipboard=text, **kwargs) is None, "text: no handover")
        check(text.reads == [] and len(list(leaf.iterdir())) == 1, "nothing read or written for text")

        with _prepared(grants):
            refusal = _outcome(paste_image.pane_image, "other", clipboard=Board(["image/png"]), **kwargs)
        check(isinstance(refusal, paste_image.PasteImageError)
              and "have not approved a desktop grant for other" in str(refusal), refusal)

        def refuse(_path, _uid, _perms):
            raise desktop_access.DesktopAccessError("the mask would widen")

        with _prepared(grants):
            failed = _outcome(paste_image.pane_image, PROJECT, clipboard=Board(["image/png"]),
                              **{**kwargs, "grant": refuse, "token": lambda: "ef" * 16})
        check(isinstance(failed, paste_image.PasteImageError) and "nothing was left behind" in str(failed), failed)
        check(sorted(p.name for p in leaf.iterdir()) == [image.name], "a failed grant leaves no image behind")

def _outcome(function, *args, **kwargs):
    """What a call returned, or the exception it raised, as a value a check can compare."""
    try:
        return function(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - reported by the check that reads it
        return exc


class _prepared:
    """`prepare_handoff_dir` as the owner's grant directory would answer it, without ACLs on a test directory."""

    def __init__(self, grants: Path) -> None:
        self.grants = grants

    def __enter__(self):
        self.real = desktop_access.prepare_handoff_dir

        def prepare(runtime, project, tenant_uid):
            leaf = Path(runtime) / "switchyard-paste" / project
            leaf.mkdir(parents=True, exist_ok=True, mode=0o700)
            return leaf

        desktop_access.prepare_handoff_dir = prepare
        return self

    def __exit__(self, *_exc) -> None:
        desktop_access.prepare_handoff_dir = self.real


def test_only_an_owner_of_another_accounts_tenant_is_offered_a_relay() -> None:
    with tempfile.TemporaryDirectory(prefix="p834o.") as tmp:
        home = Path(tmp)
        check(not paste_image.pane_handoff_offered(PROJECT, uid=ME.pw_uid, home=home), "headless: no policy")
        directory = home / ".config" / "switchyard" / "desktop-access"
        directory.mkdir(parents=True)
        (directory / f"switchyard-desktop-{ME.pw_uid}-{ME.pw_uid}-{PROJECT}.json").write_text("{}")
        check(not paste_image.pane_handoff_offered(PROJECT, uid=ME.pw_uid, home=home), "own account")
        (directory / f"switchyard-desktop-{ME.pw_uid}-0-other.json").write_text("{}")
        check(not paste_image.pane_handoff_offered(PROJECT, uid=ME.pw_uid, home=home), "another project")
        (directory / f"switchyard-desktop-{ME.pw_uid + 1}-0-{PROJECT}.json").write_text("{}")
        check(not paste_image.pane_handoff_offered(PROJECT, uid=ME.pw_uid, home=home), "another owner's")
        (directory / f"switchyard-desktop-{ME.pw_uid}-0-{PROJECT}.json").write_text("{}")
        check(paste_image.pane_handoff_offered(PROJECT, uid=ME.pw_uid, home=home), "approved for another account")
        check(not paste_image.pane_handoff_offered("dem", uid=ME.pw_uid, home=home), "a prefix of the project")


def main() -> int:
    run_module_tests(globals(), first=("test_main_hands_the_role_a_bare_ctrl_v",))
    print(f"pane_window_ctrl_v_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
