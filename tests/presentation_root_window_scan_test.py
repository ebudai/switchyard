#!/usr/bin/env python3
"""SYRD-322: the root-window check reads its own scan again, and close targeting is unchanged.

SYRD-193 gave its close scan the name of the SYRD-43 root scan, so
`unsafe_root_presentation_windows` reached a scan whose results have no `uid`.
A matching window made start, status, upgrade and replace-window raise; no
config path raised on `.parent`; `PROC_ROOT` was ignored; and the legacy
`<project>-konsole-layout.json` window was not seen at all. The two scans now
have one name each:

- **The root scan** (`presentation_terminal_windows`, filtered by
  `unsafe_root_presentation_windows`) names every terminal showing this
  project's panes as an `UnsafePresentationWindow` with the uid it runs as. It
  matches the current, configured and pre-SYRD-43 layout names and the
  project's title, as whole argument values, reads `PROC_ROOT`, and needs no
  config path.
- **The close scan** (`presentation_window_processes`) is SYRD-193's, as it
  was: this release's exact layout paths, `PresentationWindowProcess` by owner.

No real process is read or signalled: every scan is given a `/proc` tree this
test builds, every signal goes to a recorder, and no window is opened.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from scripts import presentation_windows, team_launcher  # noqa: E402
from team_launcher_test_helpers import _write_six_visible_role_config  # noqa: E402

CHECKS = 0
PROJECT = "porter"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _process(proc: Path, pid: int, argv: list[str], *, uid: int | None) -> None:
    """A /proc entry: its argv and, unless `uid` is None, the uid it runs as."""
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    if uid is not None:
        (entry / "status").write_text(
            f"Name:\t{Path(argv[0]).name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8"
        )


class _Tenant:
    """A tenant config in a temporary directory, and the paths its windows use."""

    def __init__(self, tmp: Path, project: str = PROJECT) -> None:
        self.config_path = _write_six_visible_role_config(tmp, project=project)
        self.config = team_launcher.load_project_config(project, self.config_path)
        self.current = str(team_launcher.desktop_presentation_layout_path(
            self.config, config_path=self.config_path, gui_user=""
        ))
        self.template = str(self.config.layout)
        self.team = str(team_launcher.default_layout_output_path(self.config, config_path=self.config_path))
        self.title = team_launcher.project_window_title(self.config)


class _proc_root:
    """Point the launcher's PROC_ROOT at a tree for one block, as start/status/upgrade read it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> None:
        self.saved = team_launcher.PROC_ROOT
        team_launcher.PROC_ROOT = self.path

    def __exit__(self, *exc: object) -> None:
        team_launcher.PROC_ROOT = self.saved


def _unsafe(tenant: _Tenant, proc: Path | None, *, config_path: object = "tenant") -> list[int] | str:
    path = tenant.config_path if config_path == "tenant" else config_path
    try:
        found = team_launcher.unsafe_root_presentation_windows(tenant.config, config_path=path, proc_root=proc)
    except Exception as exc:  # noqa: BLE001 - the baseline raises; the answer is what it raised
        return f"{type(exc).__name__}: {exc}"
    return [window.pid for window in found]


def test_a_root_window_on_the_current_layout_is_found_not_raised_on() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-current.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        _process(proc, 100, ["/usr/bin/konsole", "--separate", "--qwindowtitle", tenant.title,
                             "--layout", tenant.current], uid=0)
        found = team_launcher.unsafe_root_presentation_windows(
            tenant.config, config_path=tenant.config_path, proc_root=proc)
        check([type(window) for window in found] == [team_launcher.UnsafePresentationWindow],
              f"one root window, in the root check's own data model: {found!r}")
        window = found[0]
        check((window.pid, window.uid, window.user) == (100, 0, "root")
              and window.command == f"/usr/bin/konsole --separate --qwindowtitle {tenant.title} --layout "
              f"{tenant.current}", f"its pid, uid, account and command: {window!r}")
        report = team_launcher.unsafe_presentation_report(tenant.config, found)
        check(f"pid 100 (root): /usr/bin/konsole" in report
              and f"sudo switchyard replace-window {PROJECT}" in report,
              f"and the report names it and the command that replaces it: {report!r}")


def test_the_configured_process_table_is_the_one_read() -> None:
    """Start, status and upgrade pass no proc_root; they read PROC_ROOT."""
    with tempfile.TemporaryDirectory(prefix="syrd322-procroot.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        _process(proc, 110, ["konsole", "--layout", tenant.current], uid=0)
        with _proc_root(proc):
            through_config = _unsafe(tenant, None)
        with _proc_root(tmp / "empty"):
            elsewhere = _unsafe(tenant, None)
            explicit = _unsafe(tenant, proc)
    check(through_config == [110], f"PROC_ROOT is where the window is found: {through_config}")
    check(elsewhere == [], f"a PROC_ROOT without it finds nothing: {elsewhere}")
    check(explicit == [110], f"and an explicit proc_root still wins over it: {explicit}")


def test_current_and_legacy_layout_markers_are_all_this_projects() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-markers.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        shapes = {
            120: ["konsole", "--separate", "--layout", tenant.current],
            121: ["konsole", f"--layout={tenant.current}"],
            # The configured template, where it is and anywhere else.
            122: ["konsole", "--separate", "--layout", tenant.template],
            123: ["konsole", "--layout", f"/srv/elsewhere/{PROJECT}-konsole-layout.json"],
            # What releases before SYRD-43 opened: the owner's team layout,
            # under the project's title.
            124: ["konsole", "--separate", "--qwindowtitle", tenant.title, "--layout", tenant.team],
            125: ["konsole", "--layout", f"/home/old/.local/state/switchyard/projects/{PROJECT}/"
                  f"{PROJECT}-team-layout.json"],
            126: ["konsole", "--qwindowtitle", tenant.title],
            127: ["konsole", f"--qwindowtitle={tenant.title}"],
            128: ["konsole", "--layout", f"{PROJECT}-presentation-layout.json"],
        }
        for pid, argv in shapes.items():
            _process(proc, pid, argv, uid=0)
        found = _unsafe(tenant, proc)
    check(found == sorted(shapes), f"every current and legacy shape is this project's root window: {found}")


def test_another_projects_windows_and_lookalike_files_are_not_this_projects() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-others.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        others = {
            130: ["konsole", "--layout", f"/srv/old-{PROJECT}-konsole-layout.json"],
            131: ["konsole", "--layout", f"/srv/{PROJECT}-staging-presentation-layout.json"],
            132: ["konsole", "--layout", f"{tenant.current}.backup"],
            133: ["konsole", "--layout", f"{tenant.template}.backup"],
            134: ["konsole", "--qwindowtitle", f"{tenant.title} staging", "--layout", "/srv/x-team-layout.json"],
            135: ["konsole", "--workdir", f"/home/{PROJECT}"],
            136: ["konsole", "--layout", "/srv/other-konsole-layout.json"],
        }
        for pid, argv in others.items():
            _process(proc, pid, argv, uid=0)
        _process(proc, 137, ["konsole", "--layout", tenant.current], uid=0)
        found = _unsafe(tenant, proc)
    check(found == [137], f"only this project's window; a sibling's, a .backup and a lookalike title are not: {found}")


def test_root_is_told_from_the_desktop_account_and_from_a_wrapper() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-owner.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        # A correct privileged launch: sudo stays as the root parent, and the
        # terminal it started runs as the desktop account.
        _process(proc, 140, ["sudo", "-u", "desk", "-H", "--", "env", "-i", "QT_QPA_PLATFORM=wayland",
                             "/usr/bin/konsole", "--separate", "--layout", tenant.current], uid=0)
        _process(proc, 141, ["/usr/bin/konsole", "--separate", "--layout", tenant.current], uid=1000)
        # A shell that started a terminal is not the terminal.
        _process(proc, 142, ["/bin/sh", "-lc", f"konsole --separate --layout {tenant.current}"], uid=0)
        _process(proc, 143, ["/usr/bin/env", "konsole", "--layout", tenant.current], uid=0)
        # The real hazard: a terminal itself running as root.
        _process(proc, 144, ["/usr/bin/konsole", "--layout", tenant.template], uid=0)
        # A terminal whose status cannot be read is judged by its owner.
        _process(proc, 145, ["xterm", "--layout", tenant.current], uid=None)
        terminals = team_launcher.presentation_terminal_windows(
            tenant.config, config_path=tenant.config_path, proc_root=proc)
        unsafe = _unsafe(tenant, proc)
    check([(w.pid, w.uid) for w in terminals] == [(141, 1000), (144, 0), (145, os.getuid())],
          f"the terminals, each with the uid it runs as; no wrapper is a terminal: {terminals}")
    check(unsafe == [144], f"and only the one running as root is unsafe: {unsafe}")


def test_no_config_path_is_one_marker_fewer_not_a_crash() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-noconfig.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        _process(proc, 150, ["konsole", "--layout", tenant.current], uid=0)
        _process(proc, 151, ["konsole", "--layout", tenant.template], uid=0)
        _process(proc, 152, ["konsole", "--separate", "--qwindowtitle", "Another", "-e",
                             "switchyard", "--config", str(tenant.config_path)], uid=0)
        without = _unsafe(tenant, proc, config_path=None)
        defaulted = team_launcher.unsafe_root_presentation_windows(tenant.config, proc_root=proc)
        with_path = _unsafe(tenant, proc)
    check(without == [150, 151], f"no config path still finds the layout windows: {without}")
    check([w.pid for w in defaulted] == [150, 151], f"and so does leaving the argument out: {defaulted}")
    check(with_path == [150, 151, 152], f"the config path is one more marker when it is known: {with_path}")


def test_no_matching_window_and_no_readable_table_find_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-none.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        _process(proc, 160, ["konsole", "--layout", "/srv/other-presentation-layout.json"], uid=0)
        _process(proc, 161, ["/usr/bin/vim", tenant.current], uid=0)
        _process(proc, 162, ["sshd", "-D"], uid=0)
        (proc / "163").mkdir()
        (proc / "self").mkdir()
        nothing = _unsafe(tenant, proc)
        empty = proc.parent / "empty"
        empty.mkdir()
        no_entries = _unsafe(tenant, empty)
        missing = _unsafe(tenant, tmp / "missing")
    check(nothing == [], f"another project's window, an editor and an empty entry are nothing: {nothing}")
    check(no_entries == [] and missing == [], f"an empty or unreadable table is nothing: {no_entries} {missing}")


def test_status_names_the_root_window_and_says_running_otherwise() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-status.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        registry = tmp / "registry"
        registry.mkdir()
        (registry / f"{PROJECT}.json").write_text(json.dumps({
            "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA, "name": "Porter", "slug": PROJECT,
            "config_path": str(tenant.config_path),
        }), encoding="utf-8")
        configs = tmp / "configs"
        configs.mkdir()
        proc = tmp / "proc"
        _process(proc, 170, ["konsole", "--layout", tenant.current], uid=0)
        _process(proc, 171, ["konsole", "--layout", tenant.current], uid=1000)

        def status(table: Path) -> str:
            printed: list[str] = []
            with _proc_root(table):
                team_launcher.switchyard_status_command(
                    config_dir=configs, registry_dir=registry,
                    owner_tmux_reader=lambda _config: (set(), ""),
                    assignments_reader=lambda _config: ({}, ""),
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", ""),
                    print_func=printed.append,
                )
            return "\n".join(printed)

        unsafe = status(proc)
        (proc / "170" / "cmdline").unlink()
        safe = status(proc)
    check(f"{PROJECT}  unsafe-root-window" in unsafe and "presentation window(s) 170 are running as root" in unsafe
          and "171" not in unsafe and f"sudo switchyard replace-window {PROJECT}" in unsafe,
          f"status names the root window and only it: {unsafe}")
    check("unsafe-root-window" not in safe and "NOT safely attached" not in safe and "stopped" in safe,
          f"and with only the desktop account's window it reports the project truthfully: {safe}")


def test_replace_window_signals_only_this_projects_root_windows() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-replace.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        _process(proc, 180, ["konsole", "--layout", tenant.template], uid=0)
        _process(proc, 181, ["konsole", "--layout", tenant.current], uid=1000)
        _process(proc, 182, ["konsole", "--layout", f"/srv/old-{PROJECT}-konsole-layout.json"], uid=0)
        _process(proc, 183, ["sudo", "-u", "desk", "--", "konsole", "--layout", tenant.current], uid=0)
        _process(proc, 184, ["konsole", "--layout", f"{tenant.current}.backup"], uid=0)
        sent: list[tuple[int, int]] = []

        def signaller(pid: int, sig: int) -> None:
            sent.append((pid, sig))
            (proc / str(pid) / "cmdline").unlink()

        launched: list[Path] = []
        said: list[str] = []
        original = team_launcher.launch_konsole_window
        try:
            team_launcher.launch_konsole_window = lambda output, **kwargs: (launched.append(output) or 0)
            code = team_launcher.replace_presentation_window_command(
                tenant.config, config_path=tenant.config_path, proc_root=proc, euid_getter=lambda: 0,
                signaller=signaller, settle_seconds=0.2,
                runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
                print_func=said.append,
            )
        finally:
            team_launcher.launch_konsole_window = original
    check(code == 0 and sent == [(180, signal.SIGTERM)],
          f"exactly this project's root window is stopped, with SIGTERM: {code} {sent} {said}")
    check(len(launched) == 1, f"and one replacement is opened in its place: {launched}")


def test_close_targeting_is_the_exact_layout_by_owner_as_before() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd322-close.") as raw:
        tmp = Path(raw)
        tenant = _Tenant(tmp)
        proc = tmp / "proc"
        _process(proc, 190, ["konsole", "--layout", tenant.current], uid=0)
        _process(proc, 191, ["konsole", f"--layout={tenant.current}"], uid=1000)
        # The root scan's legacy markers are not close targets.
        _process(proc, 192, ["konsole", "--layout", tenant.template], uid=0)
        _process(proc, 193, ["konsole", "--qwindowtitle", tenant.title], uid=0)
        _process(proc, 194, ["konsole", "--layout", tenant.team], uid=0)
        _process(proc, 195, ["konsole", "--layout", f"{tenant.current}.backup"], uid=0)
        found = team_launcher.presentation_window_processes(
            tenant.config, config_path=tenant.config_path, proc_root=proc)
        sent: list[tuple[int, int]] = []

        def signaller(pid: int, sig: int) -> None:
            sent.append((pid, sig))
            if pid == 190:
                raise PermissionError("not yours")

        said: list[str] = []
        problems = team_launcher.close_presentation_window(
            tenant.config, config_path=tenant.config_path, proc_root=proc, signaller=signaller,
            print_func=said.append)
    check([type(w) for w in found] == [team_launcher.PresentationWindowProcess] * 2
          and [(w.pid, w.owner_uid, w.layout) for w in found]
          == [(190, os.getuid(), tenant.current), (191, os.getuid(), f"--layout={tenant.current}")],
          f"the close scan: this release's exact layout, either spelling, by owner: {found}")
    check(sent == [(190, signal.SIGTERM), (191, signal.SIGTERM)],
          f"a close signals exactly those, never a legacy or lookalike window: {sent}")
    check(problems == [f"the {PROJECT} presentation window (pid 190) belongs to uid {os.getuid()} and this "
                       "process may not close it"] and said == [f"closed presentation window: {PROJECT} (pid 191)"],
          f"and a window it may not signal is reported by its owner: {problems} {said}")


def test_each_scan_has_one_name_and_the_check_reaches_its_own() -> None:
    import ast

    tree = ast.parse((ROOT / "scripts" / "presentation_windows.py").read_text(encoding="utf-8"))
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    check(len(names) == len(set(names)), f"no function is defined twice: {names}")
    check(team_launcher.presentation_terminal_windows is presentation_windows.presentation_terminal_windows
          and team_launcher.presentation_window_processes is presentation_windows.presentation_window_processes,
          "the launcher exports both scans under their own names")
    asked: list[tuple[object, object]] = []
    original = team_launcher.presentation_terminal_windows
    try:
        team_launcher.presentation_terminal_windows = lambda config, *, config_path, proc_root: (
            asked.append((config_path, proc_root)) or [
                team_launcher.UnsafePresentationWindow(pid=1, uid=0, user="root", command="konsole"),
                team_launcher.UnsafePresentationWindow(pid=2, uid=1000, user="desk", command="konsole"),
            ])
        found = team_launcher.unsafe_root_presentation_windows(
            object(), config_path=Path("/c.json"), proc_root=Path("/p"))
    finally:
        team_launcher.presentation_terminal_windows = original
    check(asked == [(Path("/c.json"), Path("/p"))] and [w.pid for w in found] == [1],
          f"the root check asks the terminal scan through the launcher and keeps uid 0: {asked} {found}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"presentation_root_window_scan_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
