#!/usr/bin/env python3
"""SYRD-318: starting a window in the desktop account's GUI session, against the launcher it came out of.

Crossing to the desktop account, the environment it is given and the Konsole
window itself moved into `scripts/gui_window_launch.py` unchanged. This pins
what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every name the launcher and the modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **Rule 21.** Every reference the presentation controller and the desktop
  half of the hand-off make to these names goes through the launcher.
- **The patched seams are still reached.** The suites rebind
  `launch_konsole_window`, `_gui_launch_prefix`, `gui_program_path` and
  `_make_konsole_log_readable` on the launcher; the launcher calls the first
  by its own name, and nothing here calls any of them past the launcher.
- **The launcher's lookups are read when a function runs:** account ids and
  homes, and `_env_first`, which the suites rebind on the launcher.
- **The privilege boundary is unchanged:** root crosses with
  `sudo -u <account> -H --` and `env -i` plus the allowlist, and refuses to
  open a window as root or as a uid-0 account; an unprivileged caller does not
  cross.

No display, window, terminal, sudo or other account is touched: account
lookups are patched, the effective uid is passed in, the terminal process is a
fake, and every path is temporary.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'FALLBACK_XDG_CONFIG_DIRS', 'GUI_ENVIRONMENT_ALLOWLIST', 'GUI_WAYLAND_ENV', 'HOST_WAYLAND_ENV',
    'KONSOLE_DEFAULTS_NAME', 'KONSOLE_WINDOW_TITLE_DEFAULTS', 'LEGACY_GUI_WAYLAND_ENV', 'LEGACY_HOST_WAYLAND_ENV',
    'gui_environment_args', '_gui_launch_prefix', 'gui_privilege_drop_args', 'gui_program_path', '_gui_runtime_dir',
    'konsole_launch_args', 'launch_konsole_window', '_make_konsole_log_readable', 'normalize_wayland_display',
    '_print_konsole_early_exit', '_refusal_command', 'write_konsole_config_defaults',
)
READ_ELSEWHERE = {
    "presentation_controller": ("launch_konsole_window",),
    "desktop_presentation": ("launch_konsole_window", "_gui_launch_prefix", "gui_program_path",
                             "_make_konsole_log_readable", "_refusal_command"),
}
PATCHED_SEAMS = ("launch_konsole_window", "_gui_launch_prefix", "gui_program_path", "_make_konsole_log_readable")
DESK, DESK_UID = "syrd318-desk", 1318


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


def _accounts() -> dict[str, object]:
    """The launcher's account lookups, answering for accounts no host has."""
    ids = {DESK: DESK_UID, "root": 0, "syrd318-uid0": 0}
    return dict(uid_for_user=lambda user: ids.get(user), _gui_home=lambda user: f"/nonexistent/home/{user}")


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.gui_window_launch; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.gui_window_launch'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.gui_window_launch", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.gui_window_launch")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.gui_window_launch as g; "
            f"print(all(getattr(t, n) is getattr(g, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


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


def test_the_patched_seams_are_reached_through_the_launcher() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    # Two baseline sites: launch_project's, and replace_presentation_window_command's,
    # which SYRD-329 moved to presentation_window_replacement, where it still calls
    # through the launcher. SYRD-342 moved launch_project's, in its P7+P8, to
    # launch_phases, which calls it through the launcher too.
    moved_trees = [ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
                   for name in ("presentation_window_replacement.py", "launch_phases.py")]
    for name, count in {"launch_konsole_window": 2}.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        moved_calls = [n for tree in moved_trees for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) + len(moved_calls) == count and all(isinstance(n.func, ast.Name) for n in calls)
              and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id in ("launcher", "team_launcher") for n in moved_calls),
              f"{name} is called at its {count} baseline sites, by the launcher's own name there and through "
              "the launcher where a caller moved")
    moved = ast.parse((ROOT / "scripts" / "gui_window_launch.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in PATCHED_SEAMS + ("_env_first",)})
    check(bare == [], f"and nothing here reaches a rebound seam, or _env_first, past the launcher: {bare}")


def test_root_crosses_to_the_desktop_account_and_refuses_root() -> None:
    from scripts import gui_window_launch, team_launcher

    with patched(team_launcher, **_accounts()):
        unprivileged = gui_window_launch.gui_privilege_drop_args(DESK, euid=DESK_UID)
        crossing = gui_window_launch.gui_privilege_drop_args(DESK, euid=0)
        as_root = gui_window_launch.gui_privilege_drop_args("root", euid=0)
        uid_zero = gui_window_launch.gui_privilege_drop_args("syrd318-uid0", euid=0)
        unknown = gui_window_launch.gui_privilege_drop_args("syrd318-nobody", euid=0)
        environment = gui_window_launch.gui_environment_args(DESK, wayland_display="/run/user/1318/wayland-0",
                                                             config_dirs="/nonexistent/cfg:/etc/xdg")
    check(unprivileged == ([], ""), f"an unprivileged caller does not cross: {unprivileged}")
    check(crossing == (["sudo", "-u", DESK, "-H", "--"], ""), f"root crosses to the desktop account: {crossing}")
    check(as_root[0] == [] and "refusing to open a presentation window as root" in as_root[1]
          and team_launcher.GUI_USER_ENV in as_root[1], f"and refuses to stay root: {as_root}")
    check(uid_zero[0] == [] and unknown[0] == [] and "is not an unprivileged local account" in uid_zero[1]
          and "is not an unprivileged local account" in unknown[1],
          f"or to cross into a uid-0 or unknown account, by the launcher's patched lookup: {uid_zero} {unknown}")
    check(environment == ["env", "-i", f"PATH={team_launcher.DEFAULT_PANE_BASE_PATH}", f"HOME=/nonexistent/home/{DESK}",
                          f"USER={DESK}", f"LOGNAME={DESK}", "XDG_RUNTIME_DIR=/run/user/1318",
                          "XDG_CONFIG_DIRS=/nonexistent/cfg:/etc/xdg", "QT_QPA_PLATFORM=wayland",
                          "WAYLAND_DISPLAY=/run/user/1318/wayland-0"],
          f"the crossing carries exactly the allowlist, from the launcher's lookups: {environment}")


def test_the_launch_prefix_reads_the_launchers_environment_when_it_runs() -> None:
    from scripts import gui_window_launch, team_launcher

    if os.geteuid() == 0:
        return
    asked: list[tuple[str, ...]] = []

    def env_first(*names: str) -> str:
        asked.append(names)
        return "/nonexistent/syrd318/wayland" if names[0] == gui_window_launch.HOST_WAYLAND_ENV else ""

    saved = os.environ.pop("XDG_CONFIG_DIRS", None)
    try:
        with patched(team_launcher, _env_first=env_first, **_accounts()):
            prefix = gui_window_launch._gui_launch_prefix(gui_user=DESK, config_dir=Path("/nonexistent/cfg"))
        with patched(team_launcher, _env_first=lambda *names: "", **_accounts()):
            default = gui_window_launch._gui_launch_prefix(gui_user=DESK)
            nobody = gui_window_launch._gui_launch_prefix(gui_user="syrd318-nobody")
    finally:
        if saved is not None:
            os.environ["XDG_CONFIG_DIRS"] = saved
    check(asked == [("HOST_WAYLAND_DISPLAY", "PGU_HOST_WAYLAND_DISPLAY")],
          f"the host display is asked of the launcher's _env_first, as rebound: {asked}")
    check(prefix == (["env", "QT_QPA_PLATFORM=wayland", "WAYLAND_DISPLAY=/nonexistent/syrd318/wayland",
                      "XDG_CONFIG_DIRS=/nonexistent/cfg:/etc/xdg"], ""),
          f"an unprivileged caller carries only its display and the config dirs: {prefix}")
    check(default == (["env", "QT_QPA_PLATFORM=wayland", "WAYLAND_DISPLAY=/run/user/1318/wayland-0"], ""),
          f"with nothing set, the account's own wayland-0: {default}")
    check(nobody == ([], "team-launcher: no host Wayland display; run from Eric desktop session"),
          f"and an account with no uid has no display: {nobody}")


def test_konsole_argv_and_refusal_come_from_the_launchers_seams() -> None:
    from scripts import gui_window_launch, team_launcher

    layout = Path("/nonexistent/syrd318/layout.json")
    with patched(team_launcher, _gui_launch_prefix=lambda **kwargs: (["syrd318-prefix"], ""),
                 gui_program_path=lambda program: f"/opt/syrd318/{program}"):
        args = gui_window_launch.konsole_launch_args(layout, window_title=" P318 ")
    with patched(team_launcher, _gui_launch_prefix=lambda **kwargs: ([], "syrd318 cannot 'open'")):
        refusal = gui_window_launch.konsole_launch_args(layout)
    check(args == ["syrd318-prefix", "/opt/syrd318/konsole", "--separate", "--qwindowtitle", "P318",
                   "--layout", str(layout)], f"the argv is built on the launcher's patched prefix and path: {args}")
    check(refusal == ["sh", "-lc", "printf '%s\\n' 'syrd318 cannot '\"'\"'open'\"'\"'' >&2; exit 1"],
          f"and a refusal becomes a quoted command that says so and fails: {refusal}")


def test_the_konsole_window_starts_in_its_own_session_and_reports_what_happened() -> None:
    from scripts import gui_window_launch, team_launcher

    started: list[tuple[list[str], dict[str, object]]] = []
    readable: list[Path] = []

    def process(returncode: int | None, output: bytes = b""):
        def launch(args: list[str], **kwargs: object) -> object:
            started.append((list(args), dict(kwargs)))
            kwargs["stdout"].write(output)
            kwargs["stdout"].flush()
            return type("Proc", (), {"pid": 318, "poll": lambda self: returncode})()
        return launch

    def no_runner(args: list[str], **kwargs: object) -> object:
        raise AssertionError(f"a startable window must not run {args!r}")

    saved_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix="syrd318.") as raw:
        layout = Path(raw) / "state" / "p318-presentation-layout.json"
        layout.parent.mkdir()
        layout.write_text("{}", encoding="utf-8")
        layout.chmod(0o640)
        tempfile.tempdir = raw
        out, err = io.StringIO(), io.StringIO()
        try:
            with patched(team_launcher, _gui_launch_prefix=lambda **kwargs: (["syrd318-prefix"], ""),
                         gui_program_path=lambda program: f"/opt/syrd318/{program}",
                         _make_konsole_log_readable=lambda path, **kwargs: readable.append(path)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                running = gui_window_launch.launch_konsole_window(layout, project="p318", runner=no_runner,
                                                                  process_launcher=process(None))
                exited = gui_window_launch.launch_konsole_window(layout, project="p318", runner=no_runner,
                                                                 process_launcher=process(3, b"syrd318 boom\n"))
            with patched(team_launcher, _gui_launch_prefix=lambda **kwargs: ([], "syrd318 refused")):
                refused = gui_window_launch.launch_konsole_window(
                    layout, project="p318",
                    runner=lambda args, **kwargs: subprocess.CompletedProcess(args, 7, "", ""))
        finally:
            tempfile.tempdir = saved_tempdir
        defaults = layout.parent / gui_window_launch.KONSOLE_DEFAULTS_NAME
        written = defaults.read_text(encoding="utf-8")
        mode = stat.S_IMODE(defaults.stat().st_mode)
    check(running == 0 and started[0][0] == ["syrd318-prefix", "/opt/syrd318/konsole", "--separate",
                                             "--qwindowtitle", "p318", "--layout", str(layout)]
          and started[0][1]["start_new_session"] is True and started[0][1]["stdin"] == subprocess.DEVNULL,
          f"a running Konsole is started in its own session on the launcher's argv: {started[:1]!r}")
    check(len(readable) == 2 and all(path.name.startswith("p318-team-launcher-konsole.") for path in readable),
          f"its log is made readable by the launcher's patched helper: {readable!r}")
    check("started p318 in background (Konsole pid 318" in out.getvalue(), f"and it says so: {out.getvalue()!r}")
    check(exited == 3 and "Konsole exited immediately with status 3" in err.getvalue()
          and "syrd318 boom" in err.getvalue(), f"an early exit returns its status with its output: {err.getvalue()!r}")
    check(refused == 7, "a refusal runs the refusal command through the runner and returns its status")
    check(written == team_launcher.KONSOLE_WINDOW_TITLE_DEFAULTS and mode == 0o640,
          f"the window-title default sits beside the layout with the layout's mode: {mode:o}")


def test_the_log_is_handed_to_the_invoking_account() -> None:
    from scripts import gui_window_launch

    with tempfile.TemporaryDirectory(prefix="syrd318.") as raw:
        log = Path(raw) / "konsole.log"
        log.write_text("", encoding="utf-8")
        log.chmod(0o600)
        gui_window_launch._make_konsole_log_readable(
            log, environ={"SUDO_USER": "syrd318-nobody", "SUDO_UID": str(os.getuid()), "SUDO_GID": str(os.getgid())})
        mode, owner = stat.S_IMODE(log.stat().st_mode), log.stat().st_uid
    check(mode == 0o644 and owner == os.getuid(), f"the log is the invoking account's and readable: {mode:o} {owner}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"gui_window_launch_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
