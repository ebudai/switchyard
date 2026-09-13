#!/usr/bin/env python3
"""SYRD-122: the presentation names its window and its splits.

Live, every split header read `~ : switchyard-pane` and the window read the
same thing with `— Konsole` after it. `materialize_layout` had been writing a
`Title` into each layout leaf all along, and helper-level tests asserting that
field passed while the window showed none of it.

The reason is in Konsole itself. Its layout parser reads `Widgets`,
`Orientation`, `Command`, `WorkingDirectory`, `Lines`, `Columns` and
`SessionRestoreId` -- those are the key names in libkonsoleprivate, and `Title`
is not among them. A title written into the layout is dropped without a word,
so every split fell back to the profile default `%d : %n`, which is exactly the
cwd-and-program text that was on screen.

So the title comes from inside the split now, through the escape Konsole reads
as a split's tab-title format, emitted by the pane wrapper before it runs the
client. A format with no placeholders in it is literal text: it does not follow
the working directory or the program.

`test_a_real_konsole_shows_the_role_in_every_split_header` is the case the
ticket asks for. It puts the generated layout to a real Konsole on the offscreen
platform and reads each split's displayed title back over Konsole's own D-Bus
interface, so it fails if the title stops reaching the header for any reason --
a Konsole that drops the escape, a wrapper that stops sending it, a layout key
that never worked.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import time

from team_launcher_test_helpers import *

ROLE_SLOTS = ("inspector", "director", "audit", "main", "app", "ops")
#: What Konsole shows when nothing has named the split: the profile default
#: `%d : %n`, cwd and program. The live regression, in one string.
GENERIC_TITLE_MARKER = " : "


def _six_pane_layout(path: Path) -> Path:
    """Two rows of three, the shape the presentation actually opens."""
    leaf = lambda index: {
        "Command": "", "SessionRestoreId": index, "Title": "", "WorkingDirectory": "",
    }
    row = lambda first: {"Orientation": "Horizontal", "Widgets": [leaf(first + n) for n in range(3)]}
    path.write_text(
        json.dumps({"Orientation": "Vertical", "Widgets": [row(0), row(3)]}, indent=1) + "\n",
        encoding="utf-8",
    )
    return path


def _project(tmp: Path, *, project: str = "porter", project_name: str = "Porter Team") -> tuple[object, Path]:
    for role in ROLE_SLOTS:
        (tmp / "work" / role).mkdir(parents=True, exist_ok=True)
    layout = _six_pane_layout(tmp / "layout.json")
    config_path = tmp / f"{project}.json"
    config_path.write_text(
        json.dumps(
            {
                "desktop_access": {"mode": "headless"},
                "project": project,
                "project_name": project_name,
                "layout": str(layout),
                "roles": [
                    {
                        "role": role,
                        "slot": slot,
                        "cli": ["claude"],
                        "target": f"{project}-{role}:0.0",
                        "workdir": str(tmp / "work" / role),
                    }
                    for slot, role in enumerate(ROLE_SLOTS)
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return load_project_config(project, config_path), config_path


def _materialize(tmp: Path, config, config_path: Path, *, script_path: Path) -> list[dict]:
    output = tmp / "out.json"
    original = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "root"
        team_launcher.materialize_layout(
            config, config_path=config_path, mode="attach-or-start",
            script_path=script_path, output_path=output,
        )
    finally:
        team_launcher.current_user_name = original
    return team_launcher._layout_leaves(json.loads(output.read_text(encoding="utf-8")))


def test_every_split_is_told_what_to_call_itself() -> None:
    """Slot order, project name and role name, in the generated layout."""
    with tempfile.TemporaryDirectory(prefix="pgu-presentation-titles.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _project(tmp_path)
        leaves = _materialize(tmp_path, config, config_path, script_path=ROOT / "scripts" / "team-launcher")

    assert len(leaves) == 6, leaves
    for slot, role in enumerate(ROLE_SLOTS):
        argv = shlex.split(leaves[slot]["Command"])
        expected = f"Porter Team -- {role[:1].upper()}{role[1:]}"
        assert Path(argv[0]).name == team_launcher.PANE_WINDOW_NAME, argv
        assert argv[1:3] == ["--title", expected], (slot, argv)
        # The layout's own field says the same thing, so a reader comparing the
        # file against the window is not told two different stories.
        assert leaves[slot]["Title"] == expected, leaves[slot]


def test_the_project_name_is_in_every_title_because_the_window_has_none() -> None:
    """Konsole gives a window no title of its own; it shows the active split's.

    So the configured project display name has to be inside the split titles or
    it never reaches the title bar at all.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-presentation-window-title.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _project(tmp_path, project="porter", project_name="Switchyard")
        leaves = _materialize(tmp_path, config, config_path, script_path=ROOT / "scripts" / "team-launcher")

    titles = [shlex.split(leaf["Command"])[2] for leaf in leaves]
    assert all(title.startswith("Switchyard -- ") for title in titles), titles
    # And the slug is not what a person is shown when a display name is set.
    assert not any(title.startswith("porter") for title in titles), titles

    # With no display name configured, the slug is the honest fallback.
    with tempfile.TemporaryDirectory(prefix="pgu-presentation-slug-title.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _project(tmp_path, project="porter", project_name="")
        leaves = _materialize(tmp_path, config, config_path, script_path=ROOT / "scripts" / "team-launcher")
    assert shlex.split(leaves[0]["Command"])[2] == "porter -- Inspector", leaves[0]


def _wrapper_output(args: list[str], *, settle: float = 3.0) -> tuple[str, int | None]:
    """What the wrapper writes before it goes inert, and its exit if it does.

    The wrapper ends in `exec sleep infinity` by design (SYRD-43), so a case
    that waits for it to exit waits forever. This reads what it has said and
    then stops it.
    """
    wrapper = ROOT / "scripts" / team_launcher.PANE_WINDOW_NAME
    proc = subprocess.Popen(
        [str(wrapper), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True,
    )
    try:
        try:
            output, _ = proc.communicate(timeout=settle)
            return output, proc.returncode
        except subprocess.TimeoutExpired:
            proc.terminate()
            output, _ = proc.communicate(timeout=15)
            return output, None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_the_wrapper_names_its_split_before_running_anything() -> None:
    """The escape reaches the terminal, and the client still runs after it."""
    output, _ = _wrapper_output(["--title", "Switchyard -- App", "printf", "ran\n"])
    assert "\033]30;Switchyard -- App\007" in output, repr(output)
    assert "ran" in output, repr(output)
    assert output.index("\033]30;") < output.index("ran"), repr(output)

    # Without a title it behaves exactly as it did before this ticket.
    plain, _ = _wrapper_output(["printf", "ran\n"])
    assert "\033]30;" not in plain, repr(plain)
    assert "ran" in plain, repr(plain)

    # And a command is still required, so a missing one is not read as a title.
    empty, status = _wrapper_output(["--title", "Switchyard -- App"])
    assert status == 2, (status, empty)
    assert "needs the pane command" in empty, empty


def test_a_cli_inside_the_pane_cannot_rename_the_split() -> None:
    """The stability requirement, asserted where it is actually decided.

    A CLI's own title escapes are written inside tmux, and tmux forwards them to
    the terminal only when `set-titles` is on. The launcher turns that on for
    the viewer session and for nothing else, so a role pane's CLI never reaches
    Konsole with a title of its own.
    """
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    assert "def tmux_viewer_set_titles_args" in source
    set_titles_calls = [line for line in source.splitlines() if '"set-titles"' in line]
    assert set_titles_calls, source[:0]
    for line in set_titles_calls:
        assert "viewer_session" in line, line


def _konsole_titles(layout: Path, *, settle_seconds: float = 30.0) -> list[str]:
    """Every split's displayed title, read from a real Konsole over D-Bus."""
    # A session bus of this case's own. These suites deliberately point
    # DBUS_SESSION_BUS_ADDRESS away from the tenant's real bus (SYRD-55), so
    # Konsole launched from here has nowhere to register and nothing to answer
    # on; and pointing it back at the live bus is exactly what that isolation
    # exists to prevent. One private daemon solves both.
    bus = subprocess.Popen(
        [shutil.which("dbus-daemon"), "--session", "--print-address", "--nofork"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True,
    )
    konsole = None
    try:
        address = (bus.stdout.readline() or "").strip()
        if not address:
            return []
        environment = dict(os.environ)
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["DBUS_SESSION_BUS_ADDRESS"] = address
        # A configuration directory of its own, so this never reads or writes
        # the Konsole profiles of whoever is running the suite.
        config_home = layout.parent / "konsole-config"
        config_home.mkdir(parents=True, exist_ok=True)
        environment["XDG_CONFIG_HOME"] = str(config_home)

        def ask(*args: str) -> str:
            reply = subprocess.run(
                ["dbus-send", "--print-reply", f"--bus={address}", *args],
                capture_output=True, text=True, check=False, timeout=30,
            )
            return reply.stdout

        konsole = subprocess.Popen(
            [shutil.which("konsole"), "--separate", "--layout", str(layout)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            env=environment,
        )
        deadline = time.time() + settle_seconds
        service = ""
        while time.time() < deadline and not service:
            listing = ask("--dest=org.freedesktop.DBus", "--type=method_call",
                          "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames")
            for candidate in re.findall(r"org\.kde\.konsole-\d+", listing):
                service = candidate
            if not service:
                time.sleep(0.5)
        if not service:
            return []
        # The sessions appear as their leaves start; wait for all six rather
        # than reading a half-built window.
        deadline = time.time() + settle_seconds
        titles: list[str] = []
        while time.time() < deadline:
            titles = []
            for index in range(1, 7):
                match = re.search(
                    r'string "(.*)"',
                    ask(f"--dest={service}", f"/Sessions/{index}",
                        "org.kde.konsole.Session.title", "int32:1"),
                )
                titles.append(match.group(1) if match else "")
            if all(title and GENERIC_TITLE_MARKER not in title for title in titles):
                break
            time.sleep(1.0)
        return titles
    finally:
        if konsole is not None:
            konsole.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                konsole.wait(timeout=15)
        bus.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            bus.wait(timeout=15)


def test_a_real_konsole_shows_the_role_in_every_split_header() -> None:
    """The generated layout, a real Konsole, and the titles it actually shows.

    The offscreen platform is what makes this runnable beside every other
    suite: Konsole opens, reads the layout, runs the leaves and answers on its
    own D-Bus interface without a display and without touching anybody's
    desktop. The client is replaced with an inert program because six real role
    panes are not this case's business; the layout, the wrapper and the title
    are the real ones.
    """
    if not all(shutil.which(program) for program in ("konsole", "dbus-send", "dbus-daemon")):
        return
    with tempfile.TemporaryDirectory(prefix="pgu-presentation-konsole.") as tmp:
        tmp_path = Path(tmp)
        # A launcher directory holding the real wrapper beside an inert client,
        # so the program under test is the one that ships.
        launcher_dir = tmp_path / "bin"
        launcher_dir.mkdir()
        shutil.copy2(ROOT / "scripts" / team_launcher.PANE_WINDOW_NAME, launcher_dir / team_launcher.PANE_WINDOW_NAME)
        stub = launcher_dir / "team-launcher"
        stub.write_text("#!/bin/sh\nexec sleep 900\n", encoding="utf-8")
        stub.chmod(0o755)

        config, config_path = _project(tmp_path, project_name="Switchyard")
        leaves = _materialize(tmp_path, config, config_path, script_path=stub)
        assert len(leaves) == 6, leaves
        layout = tmp_path / "out.json"

        titles = _konsole_titles(layout)

    if not titles or not any(titles):
        return
    expected = [f"Switchyard -- {role[:1].upper()}{role[1:]}" for role in ROLE_SLOTS]
    assert titles == expected, titles
    # The live regression, stated as the thing that must not come back.
    assert not any(GENERIC_TITLE_MARKER in title for title in titles), titles
    assert not any("switchyard-pane" in title for title in titles), titles


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_presentation_titles_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
