#!/usr/bin/env python3
"""SYRD-317: the desktop half of the presentation hand-off and the layout writer, against the launcher they came out of.

The desktop account's half of the hand-off -- checking what crossed, choosing
a terminal and opening the window -- moved into
`scripts/desktop_presentation.py` unchanged; writing a layout into a desktop
account's home, root's no-follow crossing write included, moved into
`scripts/desktop_layout_writer.py`. This pins what makes that safe:

- **The imports point one way:** the writer imports nothing of Switchyard's;
  the desktop half imports only the layout-mode leaf; neither imports the
  launcher or the other at its top.
- Every name the launcher and the modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **Rule 21.** Every reference the presentation controller, presentation
  reconnect and upstream report make to these names goes through the launcher.
- **The patched entry points are still reached:** the bridge exec calls
  `complete_desktop_presentation` by the launcher's name, and the moved code
  reaches every rebound seam -- its own four and the launcher's -- through the
  launcher when it runs.
- **Owned fixtures, not the bridge's.** The bridge end-to-end suite is red on
  the baseline and its hand-off case does not reach this code, so the desktop
  half is driven here from a hand-off file in a directory this test owns,
  with `/usr/bin/true` as the root-owned pane program; and the no-follow walk
  runs in a home this test owns.

No display, window, terminal, sudo or other account is touched: the Konsole
launch, the terminal process, the grant and the controller's layout are
patched, and every path is temporary.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = {
    "scripts.desktop_layout_writer": ('_open_owned_directory_chain', '_write_crossing_desktop_layout',
                                      'write_desktop_layout'),
    "scripts.desktop_presentation": (
        'available_presentation_terminal', 'complete_desktop_presentation', 'launch_presentation_terminal',
        'missing_terminal_refusal', 'PRESENTATION_HANDOFF_SCHEMA', 'presentation_handoff_path',
        'presentation_pane_program_problem', 'PRESENTATION_TERMINALS', 'presentation_title_problem',
        'PRESENTATION_TITLE_MAX_LENGTH', 'PRESENTATION_TITLE_REJECTED', '_registered_project_name',
        '_tenant_has_desktop_access', 'terminal_launch_args', 'TERMINAL_RETURNS', 'TERMINAL_STAYS',
        'validated_presentation_handoff',
    ),
}
READ_ELSEWHERE = {
    "presentation_controller": ("presentation_pane_program_problem", "write_desktop_layout"),
    "presentation_reconnect": ("PRESENTATION_HANDOFF_SCHEMA",),
    "upstream_report": ("_open_owned_directory_chain",),
}
#: Rebound by the suites on the launcher, and called from inside the moved code.
PATCHED_SEAMS = ("available_presentation_terminal", "write_desktop_layout", "_tenant_has_desktop_access",
                 "complete_desktop_presentation")
PROJECT = "p317"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def loaded_after(module: str) -> list[str]:
    result = python(
        f"import sys, {module}; "
        f"print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != {module!r}))"
    )
    check(result.returncode == 0, f"{module} imports on its own: {result.stderr[-600:]}")
    return eval(result.stdout.strip())


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


def test_the_imports_point_one_way() -> None:
    check(loaded_after("scripts.desktop_layout_writer") == [], "the layout writer loads nothing of Switchyard's")
    check(loaded_after("scripts.desktop_presentation") == ["scripts.layout_modes"],
          "and the desktop half loads only the layout-mode leaf, never the launcher, the writer or the controller")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for module, names in EXPORTED.items():
        for order in ((module, "scripts.team_launcher"), ("scripts.team_launcher", module)):
            result = python(
                "import importlib; "
                f"[importlib.import_module(m) for m in {order!r}]; "
                f"import scripts.team_launcher as t, {module} as m; "
                f"print(all(getattr(t, n) is getattr(m, n) for n in {names!r}))"
            )
            check(result.stdout.strip() == "True",
                  f"{' then '.join(order)}: {module}'s names are the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_every_reader_reaches_its_names_through_the_launcher() -> None:
    for module, names in READ_ELSEWHERE.items():
        tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
        for name in names:
            check(any(name in exported for exported in EXPORTED.values()), f"{name} is exported")
            uses = [n for n in ast.walk(tree) if (isinstance(n, ast.Attribute) and n.attr == name)
                    or (isinstance(n, ast.Name) and n.id == name)]
            through = [n for n in uses if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id in ("launcher", "team_launcher")]
            check(uses and through == uses, f"{module} reads {name} through the launcher, and only there")


def test_the_patched_seams_are_reached_through_the_launcher() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(launcher_tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", ""))
             == "complete_desktop_presentation"]
    check(len(calls) == 1 and isinstance(calls[0].func, ast.Name),
          "the bridge exec calls complete_desktop_presentation at its one baseline site, by the launcher's name")
    for module in ("desktop_presentation.py", "desktop_layout_writer.py"):
        tree = ast.parse((ROOT / "scripts" / module).read_text(encoding="utf-8"))
        bare = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                       and n.id in PATCHED_SEAMS})
        check(bare == [], f"{module} never calls a rebound seam past the launcher: {bare}")


def _handoff(**changes: object) -> str:
    payload = {"schema": "switchyard.presentation-handoff.v2", "project": PROJECT, "layout": "separate",
               "slot_count": 2, "pane_program": "/usr/bin/true", "slot_titles": ["P317 main", "P317 review"],
               "window_title": "P317 window"}
    payload.update(changes)
    return json.dumps(payload)


def test_the_desktop_half_opens_the_window_from_an_owned_handoff() -> None:
    from scripts import desktop_presentation, presentation_controller, team_launcher

    konsole = next(entry for entry in team_launcher.PRESENTATION_TERMINALS if entry[0] == "konsole")
    with tempfile.TemporaryDirectory(prefix="syrd317.") as raw:
        state = Path(raw) / "state"
        state.mkdir()
        handoff = state / f"{PROJECT}-presentation-handoff.json"
        written: list[tuple[Path, dict[str, object], str]] = []
        opened: list[tuple[Path, dict[str, object]]] = []
        said: list[str] = []

        def refuse(*args: object, **kwargs: object) -> object:
            raise AssertionError(f"a refused hand-off must write and open nothing: {args!r}")

        launcher_patches = dict(
            desktop_state_dir=lambda project, user: state,
            _tenant_control_grant=lambda project, **kwargs: {"owner": "syrd317-owner"},
            available_presentation_terminal=lambda **kwargs: konsole,
            write_desktop_layout=lambda path, layout, *, gui_user, runner, project="": (
                written.append((path, layout, gui_user)) or ""),
            launch_konsole_window=lambda path, **kwargs: opened.append((path, kwargs)) or 0,
        )
        with patched(team_launcher, **launcher_patches), patched(
                presentation_controller,
                presentation_layout_payload=lambda project, **kwargs: {"syrd317": kwargs["slot_titles"]}):
            handoff.write_text(_handoff(), encoding="utf-8")
            code = desktop_presentation.complete_desktop_presentation(
                PROJECT, caller="syrd317-desktop", print_func=said.append)
            consumed = not handoff.exists()

            handoff.write_text(_handoff(schema="switchyard.presentation-handoff.v1"), encoding="utf-8")
            with patched(team_launcher, write_desktop_layout=refuse, launch_konsole_window=refuse):
                refused = desktop_presentation.complete_desktop_presentation(
                    PROJECT, caller="syrd317-desktop", print_func=said.append)

            with patched(team_launcher, _tenant_has_desktop_access=lambda project, caller="": True):
                missing_with_window = desktop_presentation.complete_desktop_presentation(
                    PROJECT, caller="syrd317-desktop", print_func=said.append)
            with patched(team_launcher, _tenant_has_desktop_access=lambda project, caller="": None):
                missing_unknown = desktop_presentation.complete_desktop_presentation(
                    PROJECT, caller="syrd317-desktop", print_func=said.append)
        expected = state / f"{PROJECT}-presentation-layout.json"
    check(code == 0 and consumed, f"a good hand-off opens the window and is consumed once: {code} {said!r}")
    check(written == [(expected, {"syrd317": ["P317 main", "P317 review"]}, "syrd317-desktop")],
          f"the layout goes to the launcher's patched writer, in the caller's state dir, as the caller: {written!r}")
    check([path for path, _ in opened] == [expected] and opened[0][1]["window_title"] == "P317 window"
          and opened[0][1]["gui_user"] is None, f"and the launcher's patched Konsole launch opens it: {opened!r}")
    check(refused == 1 and len(said) == 2 and said[0] == (
        f"switchyard: refusing {PROJECT}'s presentation handoff: "
        "the presentation handoff does not carry this schema"),
          f"a hand-off with another schema is refused before anything is written: {said!r}")
    check(missing_with_window == 1 and "no presentation window was handed back" in said[1],
          "a missing hand-off for a tenant with a window is a fault, by the launcher's patched answer")
    check(missing_unknown == 0, f"and for one that cannot be told, it is not, and says nothing: {said!r}")


def test_the_viewer_layout_runs_one_terminal_through_the_launchers_lookups() -> None:
    from scripts import desktop_presentation, presentation_controller, team_launcher

    xterm = next(entry for entry in team_launcher.PRESENTATION_TERMINALS if entry[0] == "xterm")
    started: list[list[str]] = []

    class StillRunning:
        def poll(self) -> None:
            return None

    def process_launcher(args: list[str], **kwargs: object) -> StillRunning:
        started.append(list(args))
        return StillRunning()

    def no_runner(args: list[str], **kwargs: object) -> object:
        raise AssertionError(f"the viewer path ran {args!r} instead of starting the terminal")

    said: list[str] = []
    saved_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix="syrd317.") as raw:
        state = Path(raw) / "state"
        state.mkdir()
        (state / f"{PROJECT}-presentation-handoff.json").write_text(
            _handoff(layout="viewer", slot_count=1, slot_titles=["P317"]), encoding="utf-8")
        tempfile.tempdir = raw
        try:
            with patched(team_launcher, desktop_state_dir=lambda project, user: state,
                         _tenant_control_grant=lambda project, **kwargs: {"owner": "syrd317-owner"},
                         available_presentation_terminal=lambda **kwargs: xterm,
                         _gui_launch_prefix=lambda **kwargs: (["syrd317-prefix"], ""),
                         gui_program_path=lambda name: f"/opt/syrd317/{name}",
                         _make_konsole_log_readable=lambda path, **kwargs: None,
                         untrusted_root_executable_reasons=lambda *args, **kwargs: []), patched(
                    presentation_controller,
                    display_attach_args_for=lambda project, slot, *, owner, gui_user: ["attach", owner, gui_user]):
                code = desktop_presentation.complete_desktop_presentation(
                    PROJECT, caller="syrd317-desktop", runner=no_runner, process_launcher=process_launcher,
                    print_func=said.append)
        finally:
            tempfile.tempdir = saved_tempdir
    check(code == 0 and started == [["syrd317-prefix", "/opt/syrd317/xterm", "-T", "P317 window", "-e",
                                     "attach", "syrd317-owner", "syrd317-desktop"]],
          f"one terminal, built from the launcher's prefix and program path, runs the viewer attach: {started!r}")
    check(said == [f"switchyard: opened {PROJECT}'s presentation in xterm; it shows every role in one window"],
          f"and it reports what is known: {said!r}")


def test_the_no_follow_walk_in_an_owned_home() -> None:
    from scripts import desktop_layout_writer

    uid, gid = os.getuid(), os.getgid()
    with tempfile.TemporaryDirectory(prefix="syrd317.") as raw:
        home = Path(raw)
        fd, problem, created = desktop_layout_writer._open_owned_directory_chain(home, ("a", "b"), uid=uid, gid=gid)
        try:
            opened = os.fstat(fd)
        finally:
            os.close(fd)
        modes = [stat.S_IMODE((home / sub).stat().st_mode) for sub in ("a", "a/b")]
        (home / "elsewhere").mkdir()
        (home / "link").symlink_to(home / "elsewhere")
        fd_link, link_problem, _ = desktop_layout_writer._open_owned_directory_chain(home, ("link", "x"), uid=uid,
                                                                                    gid=gid)
        linked_into = (home / "elsewhere" / "x").exists()
        fd_owner, owner_problem, _ = desktop_layout_writer._open_owned_directory_chain(home, ("a",), uid=uid + 1,
                                                                                      gid=gid)
    check(problem == "" and created == [str(home / "a"), str(home / "a" / "b")] and stat.S_ISDIR(opened.st_mode),
          f"missing components are created and the last one is opened: {problem!r} {created!r}")
    check(modes == [0o700, 0o700], f"each created 0700: {modes}")
    check(fd_link == -1 and link_problem == f"{home / 'link'} is a symlink or not a directory" and not linked_into,
          f"a symlinked component is refused and nothing is made through it: {link_problem!r}")
    check(fd_owner == -1 and owner_problem == f"{home} is owned by uid {uid}, not by uid {uid + 1}",
          f"a home another account owns is refused: {owner_problem!r}")


def test_the_writer_reads_the_launcher_when_it_runs() -> None:
    from scripts import desktop_layout_writer, team_launcher

    written: list[Path] = []
    target = Path("/nonexistent/syrd-317/layout.json")
    with patched(team_launcher, uid_for_user=lambda user: os.getuid() + 1 if user == "syrd317-other" else None,
                 current_user_name=lambda: "syrd317-me"):
        refused = desktop_layout_writer.write_desktop_layout(target, {}, gui_user="syrd317-other",
                                                             runner=subprocess.run)
        unknown = desktop_layout_writer.write_desktop_layout(target, {}, gui_user="syrd317-nobody",
                                                             runner=subprocess.run)
    if os.geteuid() != 0:
        check(refused == ("this invocation cannot give syrd317-other a readable layout: it is running as "
                          "syrd317-me, and only root can write into another account's state directory"),
              f"crossing into another account without root is refused, in the launcher's words: {refused!r}")
    check(unknown == "syrd317-nobody is not a local account", f"an unknown account is refused: {unknown!r}")
    with tempfile.TemporaryDirectory(prefix="syrd317.") as raw:
        own = Path(raw) / "state" / "layout.json"
        with patched(team_launcher, uid_for_user=lambda user: os.getuid(),
                     _write_private_json_atomic=lambda path, payload: written.append(path)):
            same = desktop_layout_writer.write_desktop_layout(own, {"a": 1}, gui_user="syrd317-self",
                                                              runner=subprocess.run)
        made = own.parent.is_dir()
    check(same == "" and written == [own] and made,
          f"the caller's own layout goes through the launcher's private writer: {same!r} {written!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"desktop_presentation_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
