"""Real-GUI integration cases: opted into, launched on one PATH, and never silently green (SYRD-338).

Two suites measure what a real offscreen Konsole does with the commands the
product stages, and both could run -- or pass -- without anyone deciding they
should:

* `legacy_presentation_launch_test` gated on `shutil.which("konsole")` in the
  caller's PATH and then launched `konsole` with the child's PATH pinned to
  `/usr/bin:/bin`. A refusing stub first on the caller's PATH passed the gate and
  was never what ran: the real Konsole opened anyway.
* `team_launcher_presentation_titles_test` returned early -- and so passed --
  whenever a program was missing or Konsole gave no reading. A refusing stub
  made exactly that happen, and four real-Konsole cases read as green having
  measured nothing.

So a real-GUI case now:

* runs only when `SWITCHYARD_GUI_INTEGRATION=1` says so. Otherwise it does not
  run, says so on its own line, and is counted in `NOT_RUN` -- never among the
  cases that passed;
* when opted in, requires its programs: a missing one fails the case rather
  than skipping it, and so does a reading that comes back empty;
* resolves each program on the SAME PATH its child is given (the caller's), so
  whatever that PATH says -- a refusing stub included -- is what runs;
* launches only through `launch`, which refuses outright when not opted in, and
  fails the case if anything it started is still running when the case ends.

What these cases need to run, exactly: `konsole` (and, for the titles suite,
`dbus-daemon`, `dbus-send`, and `tmux` for its tmux case) on PATH, and nothing
else of the host's -- each case gives its child its own HOME, runtime and XDG
directories, the offscreen Qt platform, and (titles) a private session bus.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

OPT_IN = "SWITCHYARD_GUI_INTEGRATION"
#: The real-GUI cases that did not run in this process, by name.
NOT_RUN: list[str] = []
#: What `launch` started, so a case can be failed for leaving any of it behind.
_STARTED: list[subprocess.Popen] = []


class GuiNotPermitted(AssertionError):
    """A real GUI program was about to start without the opt-in."""


def opted_in() -> bool:
    return os.environ.get(OPT_IN, "") == "1"


def child_environment(sandbox: Path, **extra: str) -> dict[str, str]:
    """What a GUI child is given: the caller's PATH, and a home and desktop of the case's own."""
    env = {
        # The caller's PATH, not a pinned one: programs are resolved on exactly
        # this, so the program that runs is the one that was checked for.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(sandbox / "home"),
        "XDG_RUNTIME_DIR": str(sandbox / "run"),
        "XDG_CONFIG_HOME": str(sandbox / "cfg"),
        "XDG_DATA_HOME": str(sandbox / "data"),
        "XDG_CACHE_HOME": str(sandbox / "cache"),
        "QT_QPA_PLATFORM": "offscreen",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for key in ("home", "run", "cfg", "data", "cache"):
        (sandbox / key).mkdir(parents=True, exist_ok=True)
    (sandbox / "run").chmod(0o700)
    env.update(extra)
    return env


def resolve(program: str, env: Mapping[str, str]) -> str | None:
    """Where `program` is on the child's own PATH -- the one it will be started with."""
    if os.path.isabs(program):
        return program if os.access(program, os.X_OK) else None
    return shutil.which(program, path=env.get("PATH", ""))


def launch(argv: Sequence[str], env: Mapping[str, str], **popen_kwargs: Any) -> subprocess.Popen:
    """Start a real GUI program: only when opted in, and resolved on its own PATH."""
    if not opted_in():
        raise GuiNotPermitted(
            f"refused to start {argv[0]}: real GUI integration is opt-in ({OPT_IN}=1)"
        )
    program = resolve(str(argv[0]), env)
    if program is None:
        raise AssertionError(f"{argv[0]} is not on the PATH its child is given: {env.get('PATH')}")
    proc = subprocess.Popen([program, *map(str, argv[1:])], env=dict(env), **popen_kwargs)
    _STARTED.append(proc)
    return proc


def gui_case(*programs: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    """Mark a case as real-GUI integration: opted into, its programs required, nothing left running."""

    def wrap(case: Callable[[], Any]) -> Callable[[], Any]:
        @functools.wraps(case)
        def run() -> Any:
            if not opted_in():
                NOT_RUN.append(case.__name__)
                print(f"  NOT RUN {case.__name__}: real GUI integration is opt-in "
                      f"(set {OPT_IN}=1; it needs {', '.join(programs)} on PATH)", flush=True)
                return None
            missing = [program for program in programs if shutil.which(program) is None]
            if missing:
                raise AssertionError(f"{case.__name__}: opted in, but {', '.join(missing)} not on PATH")
            before = len(_STARTED)
            try:
                return case()
            finally:
                left = [proc for proc in _STARTED[before:] if proc.poll() is None]
                for proc in left:
                    proc.kill()
                    proc.wait(timeout=10)
                del _STARTED[before:]
                if left:
                    raise AssertionError(f"{case.__name__} left {len(left)} GUI process(es) running: "
                                         f"{[proc.args[0] for proc in left]}")

        run.gui_integration = True  # type: ignore[attr-defined]
        return run

    return wrap


def summary() -> str:
    """One line a suite prints last, so a run that measured nothing real cannot read as one that did."""
    if not NOT_RUN:
        return ""
    return (f"{len(NOT_RUN)} real-GUI integration case(s) NOT RUN (opt in with {OPT_IN}=1): "
            + ", ".join(NOT_RUN))


def require_reading(what: str, reading: Sequence[Any]) -> None:
    """An opted-in real-GUI case that read nothing has measured nothing: that is a failure."""
    if not reading or not any(reading):
        raise AssertionError(f"the real Konsole gave no {what}; an empty reading is not a pass")
