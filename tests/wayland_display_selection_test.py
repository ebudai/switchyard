#!/usr/bin/env python3
"""Which Wayland display a presentation window opens on (SYRD-621).

After a host outage the desktop owner's KWin came back on `wayland-1`, and a
plain `switchyard otto` from that desktop still started Konsole on the forced
`wayland-0`: Konsole aborted with status -6 and an empty log. These cases use
real listening Unix sockets in a fixture runtime directory for this process's
own account (`wayland_selection.RUN_USER_ROOT`), and every override variable is
set or cleared per case, so nothing on the host decides an answer.
"""

from __future__ import annotations

import contextlib
import io
import os
import pwd
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import desktop_presentation, gui_window_launch, team_launcher as launcher, wayland_selection  # noqa: E402

ME = pwd.getpwuid(os.getuid()).pw_name
UID = os.getuid()
OVERRIDES = ("HOST_WAYLAND_DISPLAY", "PGU_HOST_WAYLAND_DISPLAY", "TEAM_LAUNCHER_WAYLAND_DISPLAY",
             "PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", "WAYLAND_DISPLAY")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


@contextlib.contextmanager
def desktop(*live: str, env: dict[str, str] | None = None, mode: int = 0o700):
    """A runtime directory for this account with `live` sockets listening, and exactly `env` set."""
    with tempfile.TemporaryDirectory(prefix="syrd621.") as raw:
        runs = Path(raw) / "run-user"
        runtime = runs / str(UID)
        runtime.mkdir(parents=True)
        listeners = []
        for name in live:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(runtime / name))
            listeners.append(listener)
        runtime.chmod(mode)
        saved = {name: os.environ.pop(name, None) for name in OVERRIDES}
        os.environ.update(env or {})
        try:
            with patch.object(wayland_selection, "RUN_USER_ROOT", runs):
                yield runtime
        finally:
            runtime.chmod(0o700)
            for listener in listeners:
                listener.close()
            for name in OVERRIDES:
                os.environ.pop(name, None)
                if saved[name] is not None:
                    os.environ[name] = saved[name]


def chosen(*, privileged: bool = False) -> tuple[str | None, str]:
    return wayland_selection.select_wayland_display(ME, privileged=privileged)


def test_the_desktop_shells_own_display_is_used() -> None:
    with desktop("wayland-1", env={"WAYLAND_DISPLAY": "wayland-1"}) as runtime:
        check(chosen() == (str(runtime / "wayland-1"), ""), f"the session's own wayland-1, not wayland-0: {chosen()}")
        prefix = gui_window_launch._gui_launch_prefix(gui_user=ME)
        check(prefix == (["env", "QT_QPA_PLATFORM=wayland", f"WAYLAND_DISPLAY={runtime / 'wayland-1'}"], ""),
              f"and that is what Konsole is started on: {prefix}")
    with desktop("wayland-0", "wayland-1", env={"WAYLAND_DISPLAY": "wayland-1"}) as runtime:
        check(chosen() == (str(runtime / "wayland-1"), ""),
              f"with a greeter's wayland-0 live as well, the session says which: {chosen()}")
    with desktop("wayland-0", "wayland-1") as runtime:
        os.environ["WAYLAND_DISPLAY"] = str(runtime / "wayland-1")
        check(chosen() == (str(runtime / "wayland-1"), ""),
              f"an absolute path in its own runtime too, chosen over another live one: {chosen()}")


def test_a_stale_or_absent_display_falls_back_to_the_only_live_socket() -> None:
    for env, label in (({"WAYLAND_DISPLAY": "wayland-0"}, "a stale wayland-0"), ({}, "no WAYLAND_DISPLAY")):
        with desktop("wayland-1", env=env) as runtime:
            check(chosen() == (str(runtime / "wayland-1"), ""), f"{label}: the account's only live socket: {chosen()}")
    with desktop(env={"WAYLAND_DISPLAY": "wayland-0"}) as runtime:
        display, why = chosen()
        check(display is None and why == (
            f"team-launcher: no live Wayland socket for {ME} in {runtime}. This session's WAYLAND_DISPLAY=wayland-0 was "
            f"not used: {runtime / 'wayland-0'} is not live. Log into that account's desktop session, or name its "
            "display with TEAM_LAUNCHER_WAYLAND_DISPLAY=<wayland-N>."),
              f"nothing live is refused, naming what was tried and what to do: {why}")


def test_explicit_overrides_keep_their_precedence() -> None:
    with desktop("wayland-1", "wayland-7", env={"WAYLAND_DISPLAY": "wayland-1", "TEAM_LAUNCHER_WAYLAND_DISPLAY": "wayland-7"}) as runtime:
        check(chosen() == (str(runtime / "wayland-7"), ""), f"the Switchyard override beats the session's: {chosen()}")
        os.environ["HOST_WAYLAND_DISPLAY"] = str(runtime / "wayland-1")
        check(chosen() == (str(runtime / "wayland-1"), ""), f"and the host display beats both: {chosen()}")
    with desktop("wayland-1", "wayland-5", env={"PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY": "wayland-5"}) as runtime:
        check(chosen() == (str(runtime / "wayland-5"), ""), f"legacy names still count: {chosen()}")


def test_an_override_is_held_to_the_same_proof_and_refused_by_name() -> None:
    """Audit, 13:42: an override's precedence is not a pass around the checks every other display gets."""
    with desktop("wayland-1") as runtime:
        elsewhere = Path(tempfile.mkdtemp(prefix="syrd621-elsewhere."))
        try:
            outside = socket.socket(socket.AF_UNIX)
            outside.bind(str(elsewhere / "wayland-3"))
            (runtime / "wayland-2").symlink_to(runtime / "wayland-1")
            cases = (
                ("TEAM_LAUNCHER_WAYLAND_DISPLAY", "/tmp/not-a-wayland-socket",
                 f"/tmp/not-a-wayland-socket is not a Wayland socket in {runtime}"),
                ("TEAM_LAUNCHER_WAYLAND_DISPLAY", str(elsewhere / "wayland-3"),
                 f"{elsewhere / 'wayland-3'} is not a Wayland socket in {runtime}"),
                ("TEAM_LAUNCHER_WAYLAND_DISPLAY", "wayland-7", f"{runtime / 'wayland-7'} is not live"),
                ("TEAM_LAUNCHER_WAYLAND_DISPLAY", "wayland-2", f"{runtime / 'wayland-2'} is a symbolic link"),
                ("HOST_WAYLAND_DISPLAY", "/srv/host/w", f"/srv/host/w is not a Wayland socket in {runtime}"),
                ("PGU_HOST_WAYLAND_DISPLAY", "wayland-9", f"{runtime / 'wayland-9'} is not live"),
            )
            for name, value, problem in cases:
                os.environ[name] = value
                want = (None, f"team-launcher: {name}={value} is not a display {ME} may be shown on: {problem}. "
                              f"Unset it, or name a live Wayland socket in {runtime}.")
                for privileged in (False, True):
                    check(chosen(privileged=privileged) == want,
                          f"{name}={value} (privileged={privileged}) is refused, not replaced by the live "
                          f"wayland-1: {chosen(privileged=privileged)}")
                os.environ.pop(name)
            outside.close()
        finally:
            for path in elsewhere.iterdir():
                path.unlink()
            elsewhere.rmdir()
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-7"
        refusal = gui_window_launch._gui_launch_prefix(gui_user=ME)
        check(refusal[0] == [] and refusal[1].startswith("team-launcher: TEAM_LAUNCHER_WAYLAND_DISPLAY=wayland-7 is not"),
              f"and no window is attempted: {refusal}")
        accounts = {ME: UID, "root": 0}
        with patch("os.geteuid", return_value=0), patch.object(launcher, "uid_for_user", lambda u: accounts.get(u)):
            crossing = gui_window_launch._gui_launch_prefix(gui_user=ME)
        check(crossing == refusal, f"nor across the root boundary: {crossing}")


def test_several_live_sockets_are_refused_by_name() -> None:
    for env in ({}, {"WAYLAND_DISPLAY": "wayland-9"}):
        with desktop("wayland-0", "wayland-1", env=env) as runtime:
            display, why = chosen()
            check(display is None and why.startswith(
                f"team-launcher: {ME} has several live Wayland sockets in {runtime} (wayland-0, wayland-1), and "
                "Switchyard will not choose between them.")
                  and why.endswith("set TEAM_LAUNCHER_WAYLAND_DISPLAY=<wayland-N>."),
                  f"ambiguity is refused and both are named ({env}): {why}")
            refusal = gui_window_launch._gui_launch_prefix(gui_user=ME)
            check(refusal == ([], why), f"and no window is attempted: {refusal}")


def test_only_a_safe_socket_in_the_accounts_own_runtime_is_taken() -> None:
    with desktop("wayland-1") as runtime:
        (runtime / "wayland-2").symlink_to(runtime / "wayland-1")
        os.environ["WAYLAND_DISPLAY"] = "wayland-2"
        display, _ = chosen()
        check(display == str(runtime / "wayland-1"), f"a symlink named by the session is not followed: {display}")
        elsewhere = Path(tempfile.mkdtemp(prefix="syrd621-elsewhere."))
        try:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(elsewhere / "wayland-3"))
            os.environ["WAYLAND_DISPLAY"] = str(elsewhere / "wayland-3")
            display, _ = chosen()
            check(display == str(runtime / "wayland-1"), f"nor a socket outside the account's runtime: {display}")
            listener.close()
        finally:
            for path in elsewhere.iterdir():
                path.unlink()
            elsewhere.rmdir()
        foreign = wayland_selection.socket_problem(runtime / "wayland-1", uid=UID + 1)
        check(foreign == f"{runtime / 'wayland-1'} is not a Wayland socket in {runtime.parent / str(UID + 1)}",
              f"and a socket judged for another account is not that account's: {foreign}")
    with desktop("wayland-1", mode=0o100) as runtime:
        check(not os.access(runtime, os.R_OK) and chosen() == (str(runtime / "wayland-1"), ""),
              f"a runtime that can be passed through but not listed is probed by name: {chosen()}")


def test_a_root_crossing_never_reads_the_callers_display() -> None:
    accounts = {ME: UID, "root": 0}
    with desktop("wayland-0", "wayland-1", env={"WAYLAND_DISPLAY": "wayland-1"}) as runtime:
        display, why = chosen(privileged=True)
        check(display is None and "several live Wayland sockets" in why,
              f"across the root boundary the caller's WAYLAND_DISPLAY decides nothing: {why}")
        with patch("os.geteuid", return_value=0), patch.object(launcher, "uid_for_user", lambda u: accounts.get(u)):
            crossing = gui_window_launch._gui_launch_prefix(gui_user=ME)
        check(crossing == ([], why), f"and the launch that crosses from root refuses it the same way: {crossing}")
    with desktop("wayland-1", env={"WAYLAND_DISPLAY": "wayland-0"}) as runtime:
        check(chosen(privileged=True) == (str(runtime / "wayland-1"), ""),
              f"the desktop account's only live socket serves: {chosen(privileged=True)}")
        with patch("os.geteuid", return_value=0), patch.object(launcher, "uid_for_user", lambda u: accounts.get(u)):
            prefix, refusal = gui_window_launch._gui_launch_prefix(gui_user=ME)
        check(refusal == "" and prefix[:5] == ["sudo", "-u", ME, "-H", "--"] and prefix[5:7] == ["env", "-i"]
              and f"WAYLAND_DISPLAY={runtime / 'wayland-1'}" in prefix and "WAYLAND_DISPLAY=wayland-0" not in prefix,
              f"and it is the only display that crosses, in an emptied environment: {prefix}")
    with desktop("wayland-1", "wayland-4", env={"TEAM_LAUNCHER_WAYLAND_DISPLAY": "wayland-4"}) as runtime:
        check(chosen(privileged=True) == (str(runtime / "wayland-4"), ""),
              "an operator's explicit override is still honoured as root")


def test_a_failed_launch_names_its_display_and_the_live_ones() -> None:
    # The override names a live socket the account owns, so it is taken; the
    # terminal then aborts on it anyway, as Konsole did on the outage night.
    with desktop("wayland-0", "wayland-1") as runtime:
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = str(runtime / "wayland-0")
        err = io.StringIO()
        process = SimpleNamespace(poll=lambda: -6, pid=4242)
        with tempfile.TemporaryDirectory(prefix="syrd621-layout.") as raw, contextlib.redirect_stderr(err), \
                patch.object(launcher, "_make_konsole_log_readable", lambda path: None):
            code = gui_window_launch.launch_konsole_window(
                Path(raw) / "layout.json", project="otto", gui_user=ME, process_launcher=lambda *a, **k: process)
        said = err.getvalue()
        check(code == -6 and "Konsole exited immediately with status -6" in said
              and f"team-launcher: Konsole did not open: it was started on Wayland display {runtime / 'wayland-0'}; "
                  "live Wayland sockets there: wayland-0, wayland-1." in said,
              f"Konsole's abort names the display it was given and the one that is live: {said}")
        printed: list[str] = []
        args = ["env", "QT_QPA_PLATFORM=wayland", f"WAYLAND_DISPLAY={runtime / 'wayland-0'}", "konsole", "-e", "x"]
        with patch.object(launcher, "_make_konsole_log_readable", lambda path: None):
            code = desktop_presentation.launch_presentation_terminal(
                args, project="otto", terminal=("konsole", "-e", "--qwindowtitle", "stays"),
                process_launcher=lambda *a, **k: process, print_func=printed.append)
        check(code == -6 and any(line.startswith(f"switchyard: it was started on Wayland display {runtime / 'wayland-0'}; "
                                                 "live Wayland sockets there: wayland-0, wayland-1.") for line in printed),
              f"and so does the presentation terminal's: {printed}")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"wayland_display_selection_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
