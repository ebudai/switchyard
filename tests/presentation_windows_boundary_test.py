#!/usr/bin/env python3
"""SYRD-321: finding and closing presentation windows, against the launcher they came out of.

Finding a project's presentation windows among running processes and closing
them moved into `scripts/presentation_windows.py` unchanged -- including BOTH
definitions of `presentation_window_processes`, in their original order. This
pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- **The same definition binds (rule 12).** The name is defined twice, and the
  later definition is the one the launcher's name, the module's name and every
  caller reach -- exactly as in the launcher, where the later one shadowed the
  earlier since SYRD-193.
- Every exported name is the very same object whichever module is imported
  first, and every reader goes through the launcher (rule 21).
- **Seams (rule 24).** The suites rebind `close_desktop_presentation` on the
  launcher; the launcher calls every entry point by its own name, and the
  module reaches the scans through the launcher.
- **What it does is unchanged:** whole-argument matching of the layout path in
  either spelling, Konsole only, the owner's uid; SIGTERM to exactly those pids;
  what a close could not do is reported.
- **A baseline defect is pinned, not fixed.** `unsafe_root_presentation_windows`
  was written against the earlier definition and reaches the later one, so a
  matching window makes it raise. SYRD-321 reported it; a fix must change the
  check below on purpose.

No real process is read or signalled: every scan is given a `/proc` tree this
test builds, and the signaller is a recorder.
"""

from __future__ import annotations

import ast
import inspect
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'PRESENTATION_PROGRAM_NAMES', 'close_desktop_presentation', 'close_presentation_window',
    'desktop_presentation_windows', 'presentation_layout_markers', 'presentation_window_processes',
    'PresentationWindowProcess', '_proc_cmdline', 'unsafe_presentation_report', 'unsafe_root_presentation_windows',
    'UnsafePresentationWindow',
)
READ_ELSEWHERE = {
    "presentation_controller": ("presentation_window_processes",),
    "project_status": ("unsafe_root_presentation_windows",),
}
#: Measured on the baseline outside the moved code, and unchanged in number: the
#: launcher's own, and those of replace_presentation_window_command, which SYRD-329
#: moved to presentation_window_replacement, where it still calls through the launcher.
#: SYRD-344 moved launch_project's P9, holding one scan and one report, to
#: launch_phases, which calls them through the launcher too.
LAUNCHER_CALLS = {"unsafe_root_presentation_windows": 5, "unsafe_presentation_report": 4,
                  "close_presentation_window": 1, "close_desktop_presentation": 1}
MOVED_CALLERS = ("presentation_window_replacement.py", "launch_phases.py")
PATCHED_SEAMS = ("presentation_window_processes", "desktop_presentation_windows", "close_desktop_presentation",
                 "unsafe_root_presentation_windows")


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


def _process(proc: Path, pid: object, argv: list[str] | None) -> None:
    entry = proc / str(pid)
    entry.mkdir()
    if argv is not None:
        (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")


def _signaller(outcomes: dict[int, BaseException]):
    sent: list[tuple[int, int]] = []

    def signaller(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        if pid in outcomes:
            raise outcomes[pid]
    return signaller, sent


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.presentation_windows; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.presentation_windows'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.presentation_windows", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.presentation_windows")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.presentation_windows as w; "
            f"print(all(getattr(t, n) is getattr(w, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_a_binding_is_the_later_definition_as_in_the_launcher() -> None:
    # Named to run first: the binding is checked before any scan could fail on it.
    from scripts import presentation_windows, team_launcher

    tree = ast.parse((ROOT / "scripts" / "presentation_windows.py").read_text(encoding="utf-8"))
    copies = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "presentation_window_processes"]
    check(len(copies) == 2 and copies[0].lineno < copies[1].lineno, "both definitions moved, in their order")
    bound = team_launcher.presentation_window_processes
    check(bound is presentation_windows.presentation_window_processes
          and bound.__code__.co_firstlineno == copies[1].lineno,
          "the launcher's name and the module's are the later definition")
    check(bound.__annotations__["return"] == "list[PresentationWindowProcess]"
          and inspect.signature(bound).parameters["config_path"].default is inspect.Parameter.empty,
          "the SYRD-193 scan: PresentationWindowProcess results, config_path required")
    launcher_defs = [n for n in ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")).body
                     if isinstance(n, ast.FunctionDef) and n.name == "presentation_window_processes"]
    check(launcher_defs == [], "and no copy is left in the launcher to re-bind the name after its import")


def test_every_reader_reaches_its_names_through_the_launcher() -> None:
    for module, names in READ_ELSEWHERE.items():
        tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
        for name in names:
            check(name in EXPORTED, f"{name} is exported")
            uses = [n for n in ast.walk(tree) if (isinstance(n, ast.Attribute) and n.attr == name)
                    or (isinstance(n, ast.Name) and n.id == name)]
            through = [n for n in uses if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id in ("launcher", "team_launcher")]
            check(uses and through == uses, f"{module} reads {name} through the launcher, and only there")


def test_the_entry_points_are_reached_through_the_launcher() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    moved_trees = [ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8")) for name in MOVED_CALLERS]
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        moved_calls = [n for tree in moved_trees for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) + len(moved_calls) == count and all(isinstance(n.func, ast.Name) for n in calls)
              and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id in ("launcher", "team_launcher") for n in moved_calls),
              f"{name} is called at its {count} baseline sites, by the launcher's own name there and through "
              "the launcher where a caller moved")
    moved = ast.parse((ROOT / "scripts" / "presentation_windows.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in PATCHED_SEAMS})
    check(bare == [], f"and nothing here calls a scan or close past the launcher: {bare}")


def _layout_paths(root: Path) -> dict[str, object]:
    desk = root / "desk" / "p321-presentation-layout.json"
    tenant = root / "tenant" / "p321-team-layout.json"
    return dict(desktop_presentation_layout_path=lambda config, *, config_path, gui_user: desk,
                default_layout_output_path=lambda config, *, config_path: tenant), desk, tenant


def test_a_projects_windows_are_found_by_whole_argument() -> None:
    from scripts import presentation_windows, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd321.") as raw:
        root = Path(raw)
        paths, desk, tenant = _layout_paths(root)
        proc = root / "proc"
        proc.mkdir()
        _process(proc, 100, ["/usr/bin/konsole", "--separate", "--layout", str(desk)])
        _process(proc, 101, ["konsole", f"--layout={tenant.with_name('p321-presentation-layout.json')}"])
        _process(proc, 102, ["konsole", "--layout", f"{desk}.backup"])
        _process(proc, 103, ["xterm", "--layout", str(desk)])
        _process(proc, 104, ["sshd", "-D"])
        _process(proc, 105, None)
        (proc / "self").mkdir()
        config = SimpleNamespace(project="p321")
        with patched(team_launcher, **paths):
            found = presentation_windows.presentation_window_processes(config, config_path=root / "p321.json",
                                                                       proc_root=proc)
    check([(w.pid, w.owner_uid) for w in found] == [(100, os.getuid()), (101, os.getuid())],
          f"Konsole on this layout in either spelling, by owner; no .backup, no other program: {found}")
    check(found[0].layout == str(desk), f"the matched value is recorded: {found[0]}")


def test_closing_terminates_exactly_those_windows_and_reports_the_rest() -> None:
    from scripts import presentation_windows, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd321.") as raw:
        root = Path(raw)
        paths, desk, _ = _layout_paths(root)
        proc = root / "proc"
        proc.mkdir()
        for pid in (200, 201, 202, 203):
            _process(proc, pid, ["konsole", "--layout", str(desk)])
        signaller, sent = _signaller({201: ProcessLookupError(), 202: PermissionError(),
                                      203: OSError("syrd321 refused")})
        said: list[str] = []
        config = SimpleNamespace(project="p321")
        with patched(team_launcher, **paths):
            problems = presentation_windows.close_presentation_window(
                config, config_path=root / "p321.json", proc_root=proc, signaller=signaller, print_func=said.append)
            none = presentation_windows.close_presentation_window(
                config, config_path=root / "p321.json", proc_root=root / "empty", signaller=signaller,
                print_func=said.append)
    check(sent == [(pid, signal.SIGTERM) for pid in (200, 201, 202, 203)], f"SIGTERM to each match: {sent}")
    check(problems == [f"the p321 presentation window (pid 202) belongs to uid {os.getuid()} and this process "
                       "may not close it", "could not close the p321 presentation window: syrd321 refused"],
          f"one gone in between is fine; the others are reported: {problems}")
    check(said == ["closed presentation window: p321 (pid 200)", "already closed presentation window: p321"]
          and none == [], f"and it says what it closed, or that it was already closed: {said}")


def test_the_desktop_side_closes_every_window_it_finds() -> None:
    from scripts import presentation_windows, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd321.") as raw:
        root = Path(raw)
        state = root / "state"
        wanted = state / "p321-presentation-layout.json"
        proc = root / "proc"
        proc.mkdir()
        _process(proc, 300, ["konsole", "--layout", str(wanted)])
        _process(proc, 301, ["konsole", f"--layout={wanted}"])
        _process(proc, 302, ["konsole", "--layout", str(root / "other" / "p321-presentation-layout.json")])
        _process(proc, 303, ["konsole", "--layout", f"{wanted}.backup"])
        signaller, sent = _signaller({301: OSError("syrd321 refused")})
        said: list[str] = []
        with patched(team_launcher, desktop_state_dir=lambda project, user: state):
            found = presentation_windows.desktop_presentation_windows("p321", caller="syrd321-desk", proc_root=proc)
            code = presentation_windows.close_desktop_presentation("p321", caller="syrd321-desk", proc_root=proc,
                                                                  signaller=signaller, print_func=said.append)
            with patched(team_launcher, desktop_presentation_windows=lambda project, *, caller, proc_root: []):
                rebound = presentation_windows.close_desktop_presentation(
                    "p321", caller="syrd321-desk", proc_root=proc, signaller=signaller, print_func=said.append)
    check([w.pid for w in found] == [300, 301],
          f"both of this project's windows in the caller's state dir, and not a .backup's: {found}")
    check(code == 1 and sent == [(300, signal.SIGTERM), (301, signal.SIGTERM)]
          and said == ["switchyard: closed p321's presentation window (pid 300)",
                       "switchyard: could not close p321's presentation window 301: syrd321 refused"],
          f"every window is signalled, and a problem makes it exit 1: {code} {sent} {said}")
    check(rebound == 0 and len(sent) == 2, "a rebound desktop_presentation_windows is what the close asks")


def test_the_root_window_check_behaves_as_on_the_baseline() -> None:
    from scripts import presentation_windows, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd321.") as raw:
        root = Path(raw)
        paths, desk, _ = _layout_paths(root)
        proc = root / "proc"
        proc.mkdir()
        _process(proc, 400, ["konsole", "--layout", str(desk)])
        config = SimpleNamespace(project="p321")
        with patched(team_launcher, **paths):
            empty = presentation_windows.unsafe_root_presentation_windows(
                config, config_path=root / "p321.json", proc_root=root / "empty")
            try:
                presentation_windows.unsafe_root_presentation_windows(config, config_path=root / "p321.json",
                                                                      proc_root=proc)
                raised = ""
            except AttributeError as exc:
                raised = str(exc)
    check(empty == [], "no window, nothing unsafe")
    # The SYRD-321 defect, as on the baseline: the check reaches the later
    # definition, whose results have no `uid`. Fixing it must change this line.
    check(raised == "'PresentationWindowProcess' object has no attribute 'uid'",
          f"a matching window makes the check raise, exactly as on the baseline: {raised!r}")
    window = presentation_windows.UnsafePresentationWindow(pid=7, uid=0, user="root", command="konsole --layout x")
    report = presentation_windows.unsafe_presentation_report(SimpleNamespace(project="p321"), [window])
    check(report.splitlines()[1:] == ["  pid 7 (root): konsole --layout x",
                                      "An operator must run `sudo switchyard replace-window p321`, which replaces "
                                      "only those windows and leaves every worker session running."]
          and "p321 is NOT safely attached: 1 presentation window(s) are running as root" in report,
          f"the report names each window and the one command that replaces it: {report!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"presentation_windows_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
