#!/usr/bin/env python3
"""SYRD-329: `switchyard replace-window`, against the launcher it came out of.

`replace_presentation_window_command` moved into
`scripts/presentation_window_replacement.py` unchanged.
`switchyard_pane_launcher_for` did not: it is the pane launcher every window
opens, and the moved command reads it through the launcher. This pins what
makes that safe:

- **No cycle.** The module imports only the layout modes at its top.
- The launcher still exports the command, and it is the very same object
  whichever module is imported first; the launcher calls it by its own name.
- **Seams (rule 24).** The suites rebind the window launch, the layout, the
  desktop account, the pane launcher, the layout path and the window title on
  the launcher, and the controller's `presentation_enabled` on the controller:
  every launcher name the command uses is read through the launcher when it
  runs, and nothing it binds itself is read there (rule 27).
- **The replacement is unchanged:** nothing to replace is a success that
  signals nothing; a caller that is not root is refused before any signal;
  every window found is sent SIGTERM and only the ones that outlive the settle
  time SIGKILL; a lost worker is reported, never a target; the new window
  opens through the controller when the project presents through it and from
  a fresh layout otherwise; a window that fails to open is reported with its
  exit code.

Every launcher name the command calls is patched, the windows and workers are
lists this test owns, the signaller and euid are fakes and nothing waits; no
process is signalled, no window opened and no tmux server asked.
"""

from __future__ import annotations

import ast
import inspect
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = ("replace_presentation_window_command",)
#: Measured on the baseline: the launcher's own call site (the replace-window verb).
LAUNCHER_CALLS = {"replace_presentation_window_command": 1}
#: Measured on the baseline: the moved command's calls, each through the launcher.
MOVED_CALLS = {"unsafe_root_presentation_windows": 3, "unsafe_presentation_report": 2,
               "_running_project_roles": 2, "switchyard_pane_launcher_for": 1, "default_layout_output_path": 1,
               "materialize_layout": 1, "launch_konsole_window": 1, "project_window_title": 1,
               "default_gui_user": 2}
PROJECT = "p329"
LAYOUT = Path("/nonexistent/syrd329/p329-team-layout.json")
PANE_LAUNCHER = Path("/nonexistent/syrd329/switchyard-pane")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    """Rebind attributes of one module for one block, as the suites do."""

    def __init__(self, module: object, **values: object) -> None:
        self.module = module
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.module, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.module, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.module, name, value)


class Desk:
    """The launcher's names the command calls, answering from lists this test owns."""

    def __init__(self, scans: list[list[int]], workers: list[list[str]], *, gui_user: str = "syrd329-desk",
                 opened: int = 0, presents: bool = False) -> None:
        self.scans = list(scans)
        self.workers = list(workers)
        self.gui_user = gui_user
        self.opened = opened
        self.presents = presents
        self.calls: list[tuple[str, object]] = []

    def scan(self, config: object, *, config_path: Path, proc_root: Path | None) -> list[SimpleNamespace]:
        self.calls.append(("scan", (config_path, proc_root)))
        if not self.scans:
            raise AssertionError("the command scanned for windows more times than this test answers")
        return [SimpleNamespace(pid=pid) for pid in self.scans.pop(0)]

    def roles(self, config: object, *, runner: object) -> list[SimpleNamespace]:
        self.calls.append(("workers", runner))
        if not self.workers:
            raise AssertionError("the command listed the workers more times than this test answers")
        return [SimpleNamespace(role=role) for role in self.workers.pop(0)]

    def launcher_names(self) -> dict[str, object]:
        return dict(
            unsafe_root_presentation_windows=self.scan,
            unsafe_presentation_report=lambda config, windows: "REPORT " + ",".join(str(w.pid) for w in windows),
            _running_project_roles=self.roles,
            switchyard_pane_launcher_for=lambda config: PANE_LAUNCHER,
            default_layout_output_path=lambda config, *, config_path: LAYOUT,
            materialize_layout=lambda config, **kwargs: self.calls.append(("layout", kwargs)),
            launch_konsole_window=lambda output, **kwargs: self.calls.append(("konsole", (output, kwargs)))
            or self.opened,
            project_window_title=lambda config: "TITLE p329",
            default_gui_user=lambda: self.gui_user,
        )

    def controller_names(self) -> dict[str, object]:
        return dict(
            presentation_enabled=lambda config, *, config_path: self.calls.append(("enabled", config_path))
            or self.presents,
            launch_presentation=lambda config, **kwargs: self.calls.append(("controller", kwargs)) or self.opened,
        )

    def named(self, name: str) -> list[object]:
        return [value for called, value in self.calls if called == name]


def replace(desk: Desk, *, euid: int = 0, refuse: tuple[int, ...] = (), settle: float = 0.0,
            **options: object) -> tuple[int, list[tuple[int, int]], list[str]]:
    from scripts import presentation_controller, presentation_window_replacement, team_launcher

    signalled: list[tuple[int, int]] = []
    printed: list[str] = []

    def signaller(pid: int, sig: int) -> None:
        signalled.append((pid, sig))
        if pid in refuse:
            raise ProcessLookupError(f"no process {pid}")

    with patched(team_launcher, **desk.launcher_names()), patched(presentation_controller, **desk.controller_names()):
        result = presentation_window_replacement.replace_presentation_window_command(
            SimpleNamespace(project=PROJECT), config_path=Path("/nonexistent/syrd329/p329.json"),
            proc_root=Path("/nonexistent/syrd329/proc"), euid_getter=lambda: euid, signaller=signaller,
            settle_seconds=settle, runner=RUNNER, print_func=printed.append, **options)
    return result, signalled, printed


def RUNNER(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"the runner is handed on, never called here: {argv}")


def test_the_module_loads_only_the_layout_modes_at_import() -> None:
    result = python(
        "import sys, scripts.presentation_window_replacement as m; "
        "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "['scripts.layout_modes']",
          f"and loads only the layout modes, never the launcher: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.presentation_window_replacement", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.presentation_window_replacement")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.presentation_window_replacement as r; "
            f"print(all(getattr(t, n) is getattr(r, n) for n in {EXPORTED!r}), "
            "t.switchyard_pane_launcher_for.__module__)"
        )
        check(result.stdout.strip() == "True scripts.team_launcher",
              f"{' then '.join(order)}: the command is the launcher's too, and the pane launcher stayed: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_layout_default_is_the_leafs() -> None:
    from scripts import layout_modes, presentation_window_replacement, team_launcher

    default = inspect.signature(presentation_window_replacement.replace_presentation_window_command) \
        .parameters["layout_mode"].default
    check(default is layout_modes.LAYOUT_MODE_AUTO and default is team_launcher.LAYOUT_MODE_AUTO,
          f"the layout-mode default is the one object the leaf and the launcher share: {default!r}")


def test_the_calls_the_seams_and_the_functions_own_names() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) == count and all(isinstance(n.func, ast.Name) for n in calls),
              f"the launcher calls {name} at its {count} baseline site, by its own name")
    moved = ast.parse((ROOT / "scripts" / "presentation_window_replacement.py").read_text(encoding="utf-8"))
    top = [n for n in moved.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("team_launcher" in ast.dump(n) for n in top),
          "the launcher is never imported at the module's top, only when the command runs")
    for name, count in MOVED_CALLS.items():
        calls = [n for n in ast.walk(moved)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) == count and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                                          and n.func.value.id == "launcher" for n in calls),
              f"the command calls {name} at its {count} baseline sites, each through the launcher")
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in MOVED_CALLS})
    check(bare == [], f"no launcher name is read past it: {bare}")
    function = next(n for n in moved.body if isinstance(n, ast.FunctionDef))
    bound = {a.arg for a in function.args.kwonlyargs + function.args.args}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {alias.asname or alias.name for n in ast.walk(function) if isinstance(n, ast.ImportFrom)
              for alias in n.names}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check("presentation_controller" in bound and through == [],
          f"nothing the command binds itself -- the controller import least of all -- is the launcher's: {through}")


def test_nothing_to_replace_signals_nothing() -> None:
    desk = Desk(scans=[[]], workers=[["main"]])
    result, signalled, printed = replace(desk, euid=1000)
    check(result == 0 and signalled == [] and printed == [
        f"switchyard: {PROJECT} has no root-owned presentation window; nothing to replace."],
          f"no root-owned window is a success, even unprivileged, and signals nothing: {result} {printed}")
    check(desk.named("konsole") == desk.named("controller") == desk.named("layout") == [],
          "and opens nothing")


def test_a_caller_that_is_not_root_is_refused_before_any_signal() -> None:
    # Answers enough for a command that went on regardless, so the refusal itself is what is checked.
    desk = Desk(scans=[[41], [41]], workers=[["main"], ["main"]])
    result, signalled, printed = replace(desk, euid=1000)
    check(result == 1 and signalled == [], f"a tenant cannot signal a root process, so nothing is sent: {signalled}")
    check(printed == ["REPORT 41\nThis command must run as root: a tenant cannot signal a root process."],
          f"it is told what was found and why it must be root: {printed}")
    check(desk.named("konsole") == desk.named("controller") == [], "and opens nothing")


def test_every_window_is_asked_to_stop_and_the_survivors_are_killed() -> None:
    desk = Desk(scans=[[51, 52], [52]], workers=[["main", "audit"], ["main", "audit"]])
    result, signalled, printed = replace(desk, refuse=(51,))
    check(signalled == [(51, signal.SIGTERM), (52, signal.SIGTERM), (52, signal.SIGKILL)],
          f"both are sent SIGTERM, and only the one still there after the settle time SIGKILL: {signalled}")
    check(printed[0] == "REPORT 51,52"
          and printed[1] == "switchyard: could not stop presentation pid 51: no process 51",
          f"what it found is reported, and a failed signal is said, not fatal: {printed[:2]}")
    check(result == 0 and printed[-1] == (
        f"switchyard: replaced {PROJECT}'s presentation window as syrd329-desk; "
        "worker sessions still running: main, audit"), f"then it reopens and says so: {result} {printed[-1]!r}")
    check(all(runner is RUNNER for runner in desk.named("workers")) and len(desk.named("workers")) == 2,
          "the workers are listed before and after, with the caller's runner")


def test_windows_that_close_within_the_settle_time_are_not_killed() -> None:
    # Answers for a wait that never ended early; one second bounds it.
    desk = Desk(scans=[[61]] + [[]] * 40, workers=[["main"], ["main"]])
    result, signalled, printed = replace(desk, settle=1.0)
    check(result == 0 and signalled == [(61, signal.SIGTERM)],
          f"a window gone before the deadline is never sent SIGKILL, and the wait ends at once: {signalled}")
    check(len(desk.named("scan")) == 3, f"it scanned once, once while settling, once after: {desk.named('scan')}")


def test_a_lost_worker_is_reported_and_never_a_target() -> None:
    desk = Desk(scans=[[71], []], workers=[["main", "audit"], ["main"]])
    result, signalled, printed = replace(desk)
    check(signalled == [(71, signal.SIGTERM)], f"only the window is signalled: {signalled}")
    check("switchyard: worker sessions that were running are no longer running: audit. They were not a target "
          "of this command; start the project to recover them." in printed,
          f"the lost worker is named, with how to recover it: {printed}")
    check(result == 0 and printed[-1].endswith("worker sessions still running: main"),
          f"and the window still reopens: {printed[-1]!r}")


def test_the_new_window_opens_through_the_controller_when_it_presents() -> None:
    from scripts import layout_modes

    desk = Desk(scans=[[81], []], workers=[[], []], presents=True)
    launcher = object()
    result, _, _ = replace(desk, konsole_process_launcher=launcher)
    check(result == 0 and desk.named("controller") == [dict(
        config_path=Path("/nonexistent/syrd329/p329.json"), layout=layout_modes.LAYOUT_MODE_SEPARATE,
        runner=RUNNER, process_launcher=launcher)],
          f"a presenting project reopens through the controller, in separate windows: {desk.named('controller')}")
    check(desk.named("layout") == desk.named("konsole") == [], "and renders no layout of its own")


def test_the_new_window_opens_from_a_fresh_layout_otherwise() -> None:
    desk = Desk(scans=[[91], []], workers=[[], []], gui_user="")
    result, _, printed = replace(desk, pane_state_dir=Path("/nonexistent/syrd329/panes"))
    check(desk.named("layout") == [dict(config_path=Path("/nonexistent/syrd329/p329.json"), mode="attach-or-start",
                                        script_path=PANE_LAUNCHER, output_path=LAYOUT,
                                        pane_state_dir=Path("/nonexistent/syrd329/panes"))],
          f"the layout attaches or starts, with the launcher's pane launcher and path: {desk.named('layout')}")
    check(desk.named("konsole") == [(LAYOUT, dict(project=PROJECT, window_title="TITLE p329", gui_user=None,
                                                  runner=RUNNER, process_launcher=None))],
          f"Konsole opens that layout, as the desktop account or none when there is none: {desk.named('konsole')}")
    check(result == 0 and "worker sessions still running: none" in printed[-1], f"and says so: {printed[-1]!r}")
    given = Path("/nonexistent/syrd329/given-pane")
    desk = Desk(scans=[[92], []], workers=[[], []])
    replace(desk, script_path=given)
    check(desk.named("layout")[0]["script_path"] == given
          and desk.named("konsole")[0][1]["gui_user"] == "syrd329-desk",
          "a pane launcher the caller names wins, and a desktop account is passed on")


def test_a_window_that_fails_to_open_is_reported() -> None:
    desk = Desk(scans=[[95], []], workers=[["main"], ["main"]], opened=3)
    result, _, printed = replace(desk)
    check(result == 3 and printed[-1] == (
        "switchyard: replaced the root-owned window but could not open a new one (exit 3). "
        f"Worker sessions are untouched; run `switchyard {PROJECT}` from the desktop account."),
          f"the exit code is returned and the recovery said: {result} {printed[-1]!r}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_only_the_layout_modes_at_import",
             "test_either_import_order_gives_one_set_of_objects", "test_the_layout_default_is_the_leafs", "test_the_calls_the_seams_and_the_functions_own_names")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"presentation_window_replacement_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
