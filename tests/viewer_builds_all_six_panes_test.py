#!/usr/bin/env python3
"""A fresh six-role viewer is built whole, on real tmux, before success is said.

Live UAT on test11 at 743e64b: setup completed and `new` handed the window back,
then building the viewer failed -- tmux printed "no space for new pane", then
"switchyard: could not populate viewer test11-viewer", and no window opened.
Measured against a tmux 3.2a built from source (what Zorin / Ubuntu 22.04 ship):
`new-session -d -x 240 -y 80` is IGNORED for a session with no client, the
window is 80x23, and six panes made by halving the newest one run out of rows at
the fourth split. tmux 3.7 honours the flags, so nothing here ever saw it
(SYRD-221 UAT).

Built against that real 3.2a, the fixed viewer then failed one step later:
3.2a has no `window-resized` hook, and SYRD-216's relayout hook was fatal.

Every case drives real tmux on a private server. On the installed tmux, 3.2a is
modelled exactly as measured: `-x/-y` removed from `new-session`, and
`window-resized` rejected with 3.2a's own message. With
SWITCHYARD_TEST_TMUX_BINARIES naming real tmux builds (colon-separated), every
case also runs against each of them unmodified.

This pane itself runs inside tmux, and a viewer suite run from a role pane has
disconnected a live desktop before (SYRD-219). So every tmux call made here
names its server with `-S`. The viewer's panes run the product's own nested
`tmux attach`, which is bare by design; they reach the same private server
because it is started with a private `TMUX_TMPDIR`, and `-S` names exactly the
socket that resolves to. `TMUX` is removed, and the socket is checked before
anything is created or killed.
"""

from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import types
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import presentation_controller, team_launcher  # noqa: E402
from standalone_test_runner import run_module_tests  # noqa: E402

PROJECT = "sixpane"
SLOTS = 6
#: Carries the private socket to the runner and the stand-in window.
PRIVATE_SOCKET_ENV = "SYRD221_PRIVATE_TMUX_SOCKET"


@contextmanager
def private_tmux(binary: str | None = None, *, chain: bool = False):
    """A tmux server nobody else can reach, torn down whatever happens.

    Yields an environment for every process that must reach it, and a `tmux`
    function bound to it.
    """
    with tempfile.TemporaryDirectory(prefix="syrd221-viewer.") as tmp:
        tmp_path = Path(tmp)
        env = {key: value for key, value in os.environ.items() if not key.startswith("TMUX")}
        env["TMUX_TMPDIR"] = str(tmp_path)
        # Pane commands run through the server's default shell; the account's
        # login shell here is fish, which is not what a tenant owner runs.
        env["SHELL"] = "/bin/sh"
        if binary:
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            (bin_dir / "tmux").symlink_to(binary)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
        # Where a bare tmux under this TMUX_TMPDIR looks, named outright.
        socket_dir = tmp_path / f"tmux-{os.getuid()}"
        socket_dir.mkdir(mode=0o700)
        socket = str(socket_dir / "default")
        env[PRIVATE_SOCKET_ENV] = socket

        def tmux(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(["tmux", "-S", socket, "-f", "/dev/null", *args],
                                  capture_output=True, text=True, env=env)

        started = tmux("new-session", "-d", "-s", f"{PROJECT}-anchor", "sleep 600")
        assert started.returncode == 0, started.stderr
        reported = tmux("display", "-p", "#{socket_path}").stdout.strip()
        assert reported == socket, f"not the private server: {reported}"
        try:
            for slot in range(SLOTS):
                if chain:
                    # The whole presentation chain: a worker, and a display slot
                    # whose pane is the product's own proxy client of it.
                    done = tmux("new-session", "-d", "-s", f"{PROJECT}-role-{slot}", "sleep 600")
                    assert done.returncode == 0, done.stderr
                    proxy = (
                        f"env TMUX= tmux -S {socket} attach -f "
                        f"'{presentation_controller._proxy_client_flags(False)}' "
                        f"-t ={PROJECT}-role-{slot}"
                    )
                    done = tmux("new-session", "-d", "-s", f"{PROJECT}-display-{slot}", proxy)
                else:
                    done = tmux("new-session", "-d", "-s", f"{PROJECT}-display-{slot}", "sleep 600")
                assert done.returncode == 0, done.stderr
            yield env, tmux
        finally:
            # Only ever the private one, checked again at the moment of killing.
            if tmux("display", "-p", "#{socket_path}").stdout.strip() == socket:
                subprocess.run(["tmux", "-S", socket, "kill-server"],
                               capture_output=True, text=True, env=env)


def runner_for(env, *, ignore_size_flags: bool):
    """A runner that reaches the private server, optionally as tmux 3.2a would."""
    def run(args, **kwargs):
        args = list(args)
        if args and args[0] == "tmux":
            args = ["tmux", "-S", env[PRIVATE_SOCKET_ENV], *args[1:]]
        if args and args[0] == "tmux" and ignore_size_flags and "new-session" in args:
            for flag in ("-x", "-y"):
                if flag in args:
                    index = args.index(flag)
                    del args[index:index + 2]
        if args and args[0] == "tmux" and ignore_size_flags and "window-resized" in args:
            # 3.2a's answer, verbatim.
            return subprocess.CompletedProcess(args, 1, "", "invalid option: window-resized\n")
        kwargs.pop("env", None)
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        if "stdout" in kwargs or "stderr" in kwargs:
            kwargs.pop("capture_output", None)
        return subprocess.run(args, env=env, **kwargs)
    return run


def _viewer_panes(tmux) -> list[str]:
    listed = tmux("list-panes", "-t", f"={PROJECT}-viewer:0",
                  "-F", "#{pane_dead} #{pane_width}x#{pane_height} #{pane_current_command}")
    assert listed.returncode == 0, listed.stderr
    return listed.stdout.split("\n")[:-1]


def _observed_slots(tmux, *, flagged: bool = False) -> set[str]:
    """Which display sessions have a live viewer client, and with which flag.

    Asked of the clients rather than of `pane_current_command`: that reports
    the pane's default shell whenever the shell keeps the foreground -- fish
    does -- so "is it tmux" measured the account's login shell, not the viewer.
    A slot the viewer is the only view of is observed WITHOUT `ignore-size`,
    since that pane is what sizes it (SYRD-221 UAT, test12).
    """
    deadline = time.monotonic() + 5
    observed: set[str] = set()
    while time.monotonic() < deadline:
        listed = tmux("list-clients", "-F", "#{client_session} #{client_flags}").stdout
        observed = {
            line.split()[0] for line in listed.splitlines()
            if line.strip() and line.split()[0].startswith(f"{PROJECT}-display-")
            and ("ignore-size" in line.split()[1]) == flagged
        }
        if len(observed) >= SLOTS:
            break
        time.sleep(0.1)
    return observed


def _window_size(tmux) -> str:
    return tmux("display", "-p", "-t", f"={PROJECT}-viewer:0",
                "#{window_width}x#{window_height}").stdout.strip()


def _build_with_the_presentation_controller(env, *, ignore_size_flags: bool) -> None:
    config = types.SimpleNamespace(project=PROJECT)
    state = {"slot_count": SLOTS, "slots": {str(slot): f"role{slot}" for slot in range(SLOTS)}}
    presentation_controller._launch_viewer(
        config, state, runner=runner_for(env, ignore_size_flags=ignore_size_flags)
    )


def _build_with_the_launcher(env, *, ignore_size_flags: bool) -> int:
    roles = [
        types.SimpleNamespace(role=f"role{slot}", tmux_session=f"{PROJECT}-display-{slot}",
                              workdir="/")
        for slot in range(SLOTS)
    ]
    return team_launcher.launch_tmux_viewer_session(
        roles, viewer_session=f"{PROJECT}-viewer",
        runner=runner_for(env, ignore_size_flags=ignore_size_flags),
    )


def _attach_a_window(env, rows: int, cols: int, *, session: str = f"{PROJECT}-viewer"):
    """A real client, the way the person's window attaches to the viewer."""
    # It stands in for a terminal WINDOW, so it is one: a known, capable type,
    # whatever the environment running the suite says. Inherited, TERM is
    # absent under a cleared environment and `dumb` in an agent pane, and tmux
    # refuses both -- "open terminal failed: terminal does not support clear" --
    # so "did not follow its window" measured the harness (SYRD-221 DAT).
    client_env = dict(env, TERM="xterm-256color")
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - replaced by exec
        os.execvpe("tmux", ["tmux", "-S", env[PRIVATE_SOCKET_ENV], "attach", "-t",
                            f"={session}"], client_env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    os.kill(pid, signal.SIGWINCH)
    return pid, fd


def _binaries() -> list[tuple[str, str | None, bool]]:
    """(label, binary, model 3.2a on it) for every tmux this run can check."""
    cases = [
        ("installed tmux, as it is", None, False),
        ("installed tmux, as 3.2a behaves", None, True),
    ]
    for real in os.environ.get("SWITCHYARD_TEST_TMUX_BINARIES", "").split(":"):
        if real.strip():
            cases.append((f"real {real.strip()}", real.strip(), False))
    return cases


def test_the_presentation_viewer_is_built_whole_when_tmux_ignores_its_size() -> None:
    """test11's path: the presentation controller building a six-slot viewer."""
    for label, binary, model in _binaries():
        with private_tmux(binary) as (env, tmux):
            _build_with_the_presentation_controller(env, ignore_size_flags=model)
            panes = _viewer_panes(tmux)
            assert len(panes) == SLOTS, (label, panes)
            assert all(line.startswith("0 ") for line in panes), (label, "a pane died", panes)
            wanted = {f"{PROJECT}-display-{slot}" for slot in range(SLOTS)}
            assert _observed_slots(tmux) == wanted, (label, "not every slot is observed",
                                                     _observed_slots(tmux))


def test_the_launcher_viewer_is_built_whole_when_tmux_ignores_its_size() -> None:
    """The same viewer from the launcher, for a tenant with no presentation state."""
    for label, binary, model in _binaries():
        with private_tmux(binary) as (env, tmux):
            assert _build_with_the_launcher(env, ignore_size_flags=model) == 0, label
            panes = _viewer_panes(tmux)
            assert len(panes) == SLOTS, (label, panes)
            assert all(line.startswith("0 ") for line in panes), (label, "a pane died", panes)
            # This builder's panes attach the role sessions directly, as full
            # clients: each of the six must be attached.
            attached = {line.split()[0] for line in tmux(
                "list-clients", "-F", "#{client_session}").stdout.splitlines() if line.strip()}
            wanted = {f"{PROJECT}-display-{slot}" for slot in range(SLOTS)}
            deadline = time.monotonic() + 5
            while attached != wanted and time.monotonic() < deadline:
                time.sleep(0.1)
                attached = {line.split()[0] for line in tmux(
                    "list-clients", "-F", "#{client_session}").stdout.splitlines() if line.strip()}
            assert attached == wanted, (label, "not every role is shown", attached)


def test_the_built_viewer_follows_the_window_that_opens_it() -> None:
    """Pinned to build, then released: the relayout hook depends on it (SYRD-216).

    Run from each TERM a suite really meets -- an agent pane's `dumb`, and none
    at all under a cleared environment -- because the stand-in window must not
    depend on either (SYRD-221 DAT).
    """
    saved = os.environ.get("TERM")
    try:
        for term in ("dumb", None):
            if term is None:
                os.environ.pop("TERM", None)
            else:
                os.environ["TERM"] = term
            _check_the_viewer_follows_its_window()
    finally:
        if saved is None:
            os.environ.pop("TERM", None)
        else:
            os.environ["TERM"] = saved


def _check_the_viewer_follows_its_window() -> None:
    for label, binary, model in _binaries():
        for build in (_build_with_the_presentation_controller, _build_with_the_launcher):
            with private_tmux(binary) as (env, tmux):
                build(env, ignore_size_flags=model)
                assert _window_size(tmux) == (
                    f"{team_launcher.DEFAULT_VIEWER_COLUMNS}x{team_launcher.DEFAULT_VIEWER_ROWS}"
                ), (label, build.__name__, _window_size(tmux))
                pid, _fd = _attach_a_window(env, 40, 120)
                try:
                    # 40 rows, less one if this viewer keeps a status line.
                    followed = {"120x40", "120x39"}
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and _window_size(tmux) not in followed:
                        time.sleep(0.1)
                    viewer_clients = tmux("list-clients", "-t", f"={PROJECT}-viewer").stdout
                    assert viewer_clients.strip(), (label, build.__name__,
                                                    "the stand-in window never attached")
                    assert _window_size(tmux) in followed, (
                        label, build.__name__, "the viewer did not follow its window",
                        _window_size(tmux),
                    )
                    assert len(_viewer_panes(tmux)) == SLOTS, (label, build.__name__)
                finally:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)


# --- test12: the slots have to follow the window, not just the viewer --------
#
# Live UAT on test12 at e9bb37a: the GNOME window opened with all six panes,
# and maximizing it left every role at its old size inside a larger cell of
# dotted space. Measured on 3.2a, 3.4 and 3.7c alike -- so not the missing
# `window-resized` hook: the viewer's panes attached their slots `ignore-size`,
# and tmux ignores such a client whenever any unflagged client is attached on
# the server, so nothing ever sized a slot in the viewer layout (SYRD-221 UAT).


def _slot_sizes(tmux, slot: int) -> tuple[str, str, str]:
    pane = tmux("display", "-p", "-t", f"={PROJECT}-viewer:0.{slot}",
                "#{pane_width}x#{pane_height}").stdout.strip()
    display = tmux("display", "-p", "-t", f"={PROJECT}-display-{slot}:0",
                   "#{window_width}x#{window_height}").stdout.strip()
    role = tmux("display", "-p", "-t", f"={PROJECT}-role-{slot}:0",
                "#{window_width}x#{window_height}").stdout.strip()
    return pane, display, role


def _follows(pane: str, shown: str) -> bool:
    """Same width, and the height less at most the slot's own status line."""
    pw, ph = (int(v) for v in pane.split("x"))
    sw, sh = (int(v) for v in shown.split("x"))
    return sw == pw and ph - 1 <= sh <= ph


def _wait_until(check, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return check()


def _resize(pid: int, fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    os.kill(pid, signal.SIGWINCH)


def test_every_slot_follows_the_window_when_the_viewer_is_its_only_view() -> None:
    """test12, as the person saw it: attach, then maximize."""
    for label, binary, model in _binaries():
        with private_tmux(binary, chain=True) as (env, tmux):
            _build_with_the_presentation_controller(env, ignore_size_flags=model)
            pid, fd = _attach_a_window(env, 40, 120)
            try:
                for rows, cols in ((40, 120), (70, 240)):
                    _resize(pid, fd, rows, cols)

                    def all_follow() -> bool:
                        for slot in range(SLOTS):
                            pane, display, role = _slot_sizes(tmux, slot)
                            if not (_follows(pane, display) and _follows(display, role)):
                                return False
                        return True

                    assert _wait_until(all_follow), (
                        label, f"window {cols}x{rows}", "a slot did not follow its pane",
                        [_slot_sizes(tmux, slot) for slot in range(SLOTS)],
                    )
            finally:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)


def test_a_slot_in_a_window_of_its_own_keeps_that_windows_size() -> None:
    """SYRD-27's half: the viewer never resizes what a real window is showing."""
    for label, binary, model in _binaries():
        with private_tmux(binary, chain=True) as (env, tmux):
            own_pid, _own_fd = _attach_a_window(env, 44, 106, session=f"{PROJECT}-display-2")
            try:
                assert _wait_until(lambda: _slot_sizes(tmux, 2)[1].startswith("106x")), label
                _build_with_the_presentation_controller(env, ignore_size_flags=model)
                pid, fd = _attach_a_window(env, 70, 240)
                try:
                    _resize(pid, fd, 70, 240)
                    assert _wait_until(lambda: _follows(*_slot_sizes(tmux, 0)[:2])), (
                        label, "the other slots stopped following", _slot_sizes(tmux, 0))
                    time.sleep(0.5)
                    assert _slot_sizes(tmux, 2)[1].startswith("106x"), (
                        label, "the viewer resized a slot a real window shows", _slot_sizes(tmux, 2))
                finally:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            finally:
                os.kill(own_pid, signal.SIGKILL)
                os.waitpid(own_pid, 0)


def test_a_window_opened_on_a_slot_later_is_yielded_to_and_given_back() -> None:
    """Nobody reconciles here: the viewer's own client hooks must do it.

    A window opened on a slot after the viewer was built gets the slot at its
    own size, at once; when it closes, the viewer pane sizes the slot again.
    """
    for label, binary, model in _binaries():
        with private_tmux(binary, chain=True) as (env, tmux):
            _build_with_the_presentation_controller(env, ignore_size_flags=model)
            pid, fd = _attach_a_window(env, 70, 240)
            own_pid = None
            try:
                _resize(pid, fd, 70, 240)
                assert _wait_until(lambda: _follows(*_slot_sizes(tmux, 3)[:2])), label
                own_pid, _own_fd = _attach_a_window(env, 44, 106, session=f"{PROJECT}-display-3")
                _resize(pid, fd, 60, 200)  # the viewer window moves again
                assert _wait_until(lambda: _slot_sizes(tmux, 3)[1].startswith("106x")), (
                    label, "the viewer kept sizing a slot a window now shows", _slot_sizes(tmux, 3))
                assert _wait_until(lambda: _follows(*_slot_sizes(tmux, 0)[:2])), (
                    label, "the other slots stopped following", _slot_sizes(tmux, 0))
                os.kill(own_pid, signal.SIGKILL)
                os.waitpid(own_pid, 0)
                own_pid = None
                _resize(pid, fd, 70, 240)
                assert _wait_until(lambda: _follows(*_slot_sizes(tmux, 3)[:2])), (
                    label, "the slot was not given back to its viewer pane", _slot_sizes(tmux, 3))
            finally:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                if own_pid:
                    os.kill(own_pid, signal.SIGKILL)
                    os.waitpid(own_pid, 0)


def test_only_an_unknown_relayout_hook_is_forgiven() -> None:
    said: list[str] = []

    def refusing(stderr):
        return lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", stderr)

    assert team_launcher.install_viewer_relayout_hook(
        "v", runner=refusing("invalid option: window-resized\n"), print_func=said.append
    ) == 0
    assert said == [team_launcher.VIEWER_RELAYOUT_UNAVAILABLE_NOTE], said
    said.clear()
    assert team_launcher.install_viewer_relayout_hook(
        "v", runner=refusing("no server running on /tmp/x\n"), print_func=said.append
    ) != 0, "a failure that is not an unknown hook was forgiven"
    assert said == ["no server running on /tmp/x"], said


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"viewer_builds_all_six_panes_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
