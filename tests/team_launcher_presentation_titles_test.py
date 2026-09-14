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

SYRD-139 corrects what this file used to assert. The reasoning above -- that a
window has no title of its own, so the project name has to be inside every
split title -- was wrong twice over: it put `Switchyard -- ` in front of six
headers, and the window title still changed to whichever pane had focus,
because that is what Konsole's caption follows. Konsole does tell the two
apart. A split's title is OSC 30; the window title an escape sequence sets is
OSC 2, and with `ShowWindowTitleOnTitleBar` the caption reads that instead --
so every split reporting the same project name leaves the window reading it
whoever has focus. Headers are now the role alone, and
`test_a_real_konsole_keeps_the_window_title_through_every_focus_change` moves
focus across all six splits in a real Konsole and reads the window's own title
back after each one.
"""

from __future__ import annotations

import contextlib
import pwd
import re
import stat
import shutil
import subprocess
import time

from team_launcher_test_helpers import *

ROLE_SLOTS = ("inspector", "director", "audit", "main", "app", "ops")
#: The six headers the User asked for, written out rather than derived, because
#: deriving them from the code that produces them would assert nothing. An
#: implementer reads `<role> Developer`; Ops is the implementer that does not
#: (SYRD-141).
ROLE_HEADERS = {
    "inspector": "Inspector",
    "director": "Director",
    "audit": "Audit",
    "main": "Main Developer",
    "app": "App Developer",
    "ops": "Ops",
}
EXPECTED_HEADERS = [ROLE_HEADERS[role] for role in ROLE_SLOTS]
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
                        # As the projection writes it into a generated config.
                        "presentation_label": ROLE_HEADERS[role],
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
        expected = ROLE_HEADERS[role]
        assert Path(argv[0]).name == team_launcher.PANE_WINDOW_NAME, argv
        # The project names the window; the role names the split. Both cross in
        # the same command, and neither is repeated in the other (SYRD-139).
        assert argv[1:5] == ["--window-title", "Porter Team", "--title", expected], (slot, argv)
        assert "Porter Team" not in expected, expected
        # The layout's own field says the same thing, so a reader comparing the
        # file against the window is not told two different stories.
        assert leaves[slot]["Title"] == expected, leaves[slot]


def _document(**roles: str) -> dict:
    """A workflow document shaped like the canonical one, plus any overrides."""
    document = json.loads((ROOT / "examples/workflows/inspection.json").read_text(encoding="utf-8"))
    document["project"] = "porter"
    for role in document["roles"]:
        if role.get("target"):
            role["target"] = f"porter-{role['name']}:0.0"
        role.pop("presentation_label", None)
        if role["name"] in roles:
            role["presentation_label"] = roles[role["name"]]
    return document


def _projected_labels(document: dict) -> dict[str, str]:
    from scripts.workflow_launcher import project_roles

    raw = {
        "project": "porter",
        "repository": "/tmp/porter",
        "roles": [
            {
                "role": role["name"],
                "cli": [role["runtime"]],
                "target": role["target"],
                "tmux_session": role["target"].split(":")[0],
            }
            for role in document["roles"]
            if role.get("runtime")
        ],
    }
    return {
        role["role"]: role.get("presentation_label", "")
        for role in project_roles(raw, document)["roles"]
    }


def test_provisioning_gives_implementers_the_developer_label_and_ops_its_own() -> None:
    """Acceptance 2 and 3, decided where the role's kind is known.

    The default follows the KIND the document declares, so an implementer
    invented next week reads `<role> Developer` without anyone editing code --
    and nothing anywhere lists this tenant's role names to decide it. Ops is
    the exception provisioning ships, by name, because it IS a name; and a
    tenant that disagrees with either says so in its own document, which wins
    over both.
    """
    labels = _projected_labels(_document())
    for role, expected in ROLE_HEADERS.items():
        assert labels[role] == expected, (role, labels)

    # A tenant's own override beats the implementer default AND the shipped
    # exception, in both directions.
    overridden = _projected_labels(_document(main="Main", ops="Ops Developer"))
    assert overridden["main"] == "Main", overridden
    assert overridden["ops"] == "Ops Developer", overridden

    # And turning an override off returns the role to the computed default,
    # rather than leaving last week's answer in the generated file.
    assert _projected_labels(_document())["main"] == "Main Developer"


def test_a_new_implementer_inherits_the_developer_label() -> None:
    """Future ephemeral implementers, without a code change.

    The role added here does not exist in any shipped document or table; it is
    declared an implementer and that is the whole of what decides its label.
    """
    document = _document()
    template = next(role for role in document["roles"] if role["name"] == "app")
    invented = dict(template, name="mefp", label="Mefp", target="porter-mefp:0.0", slot=None)
    invented.pop("slot", None)
    document["roles"].append(invented)
    labels = _projected_labels(document)
    assert labels["mefp"] == "Mefp Developer", labels
    # A reviewer added the same way does not inherit it.
    reviewer = dict(template, name="checker", label="Checker", kind="reviewer",
                    target="porter-checker:0.0")
    reviewer.pop("slot", None)
    document["roles"].append(reviewer)
    assert _projected_labels(document)["checker"] == "Checker", _projected_labels(document)


def test_the_project_names_the_window_and_the_role_names_the_split() -> None:
    """Two names, from two configured facts, and neither borrows the other.

    The display name is the project's and reaches the title bar as the window
    title every split reports; the role's name is the split's and reaches its
    header. A tenant that configures neither gets its slug and its slugs, which
    is the honest fallback, and nothing here is spelled for one tenant.
    """
    def command_titles(project: str, project_name: str) -> tuple[list[str], list[str]]:
        with tempfile.TemporaryDirectory(prefix="pgu-presentation-window-title.") as tmp:
            tmp_path = Path(tmp)
            config, config_path = _project(tmp_path, project=project, project_name=project_name)
            leaves = _materialize(
                tmp_path, config, config_path, script_path=ROOT / "scripts" / "team-launcher"
            )
        argvs = [shlex.split(leaf["Command"]) for leaf in leaves]
        return [argv[2] for argv in argvs], [argv[4] for argv in argvs]

    windows, splits = command_titles("porter", "Switchyard")
    assert windows == ["Switchyard"] * 6, windows
    assert splits == EXPECTED_HEADERS, splits
    # The regression in one line: no header repeats the project.
    assert not any("Switchyard" in title for title in splits), splits
    assert not any(title.startswith("porter") for title in splits), splits

    # With no display name configured, the slug is the honest fallback -- and it
    # is the WINDOW that falls back to it, not the headers.
    windows, splits = command_titles("porter", "")
    assert windows == ["porter"] * 6, windows
    assert splits[0] == "Inspector", splits

    # Another tenant, another set of roles: nothing here is tenant-specific.
    assert command_titles("otto", "Otto Works")[0] == ["Otto Works"] * 6


def test_a_pane_that_failed_to_start_still_names_the_window() -> None:
    """The one pane that does not go through the wrapper.

    A failed role's tab runs its own message and waits. If it reports no window
    title, Konsole falls back to that split's title for the caption the moment
    somebody clicks it -- the window following focus again, in exactly the
    situation where a person is clicking around to find out what went wrong.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-failed-role-title.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _project(tmp_path, project_name="Switchyard")
        output = tmp_path / "out.json"
        original = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "root"
            team_launcher.materialize_layout(
                config, config_path=config_path, mode="attach-or-start",
                script_path=ROOT / "scripts" / "team-launcher", output_path=output,
                failed_roles={"ops": "checkout is dirty"},
            )
        finally:
            team_launcher.current_user_name = original
        leaves = team_launcher._layout_leaves(json.loads(output.read_text(encoding="utf-8")))

    failed = leaves[ROLE_SLOTS.index("ops")]["Command"]
    assert "checkout is dirty" in failed, failed
    assert "\\033]2;%s\\007" in failed and "Switchyard" in failed, failed
    # It is still inert, which is the older rule this must not break (SYRD-43).
    assert "exec sleep infinity" in failed, failed


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
    output, _ = _wrapper_output(["--window-title", "Switchyard", "--title", "App", "printf", "ran\n"])
    assert "\033]30;App\007" in output, repr(output)
    # The window's name, through the escape Konsole reads as the window title
    # rather than the split's, and before the split's so a terminal that only
    # understood one of them would still be told which (SYRD-139).
    assert "\033]2;Switchyard\007" in output, repr(output)
    assert output.index("\033]2;") < output.index("\033]30;"), repr(output)
    assert "ran" in output, repr(output)
    assert output.index("\033]30;") < output.index("ran"), repr(output)

    # Said twice, a moment apart. A terminal still attaching its emulation can
    # drop the first bytes it is sent, and offscreen Konsole dropped them about
    # half the time -- losing the window title while keeping the split title
    # sent right after it, which is a window named after its own command line
    # beside six correctly named panes. Repeating makes a lost first write stop
    # mattering (SYRD-141).
    assert output.count("\033]2;Switchyard\007") == 2, repr(output)
    assert output.count("\033]30;App\007") == 2, repr(output)
    assert output.index("ran") > output.rindex("\033]2;"), repr(output)

    # Either title alone is still accepted, and neither invents the other.
    split_only, _ = _wrapper_output(["--title", "App", "printf", "ran\n"])
    assert "\033]30;App\007" in split_only and "\033]2;" not in split_only, repr(split_only)
    window_only, _ = _wrapper_output(["--window-title", "Switchyard", "printf", "ran\n"])
    assert "\033]2;Switchyard\007" in window_only and "\033]30;" not in window_only, repr(window_only)

    # Without a title it behaves exactly as it did before this ticket.
    plain, _ = _wrapper_output(["printf", "ran\n"])
    assert "\033]30;" not in plain, repr(plain)
    assert "ran" in plain, repr(plain)

    # And a command is still required, so a missing one is not read as a title.
    empty, status = _wrapper_output(["--window-title", "Switchyard", "--title", "App"])
    assert status == 2, (status, empty)
    assert "needs the pane command" in empty, empty


def test_every_session_that_names_the_terminal_names_the_project() -> None:
    """Where the terminal title is decided, in both files that decide it.

    This case used to read only `team_launcher.py` and conclude that
    `set-titles` is on for the viewer session "and for nothing else". It is
    not: `presentation_controller.py` turns it on for every display slot, and
    the string it sent named the slot and the role. That is how the caption
    came to read `syrd slot 1: director` while this suite passed -- the claim
    was true of the file it looked in and false of the system (SYRD-141).

    So both files are read, and what is asserted is the property that matters:
    every session that forwards a title to the terminal forwards the project's
    window title, and no `set-titles-string` anywhere is built from a role or a
    slot.
    """
    sources = {
        name: (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in ("team_launcher.py", "presentation_controller.py")
    }
    assert "def tmux_viewer_set_titles_args" in sources["team_launcher.py"]
    assert "def display_slot_terminal_title_commands" in sources["presentation_controller.py"]

    enabling = [
        (name, line)
        for name, source in sources.items()
        for line in source.splitlines()
        if '"set-titles"' in line and "assert" not in line
    ]
    assert len(enabling) == 2, enabling
    # On, not off. Silencing the forwarding would also leave the caption alone
    # today -- but only because the wrapper happens to have spoken first, and
    # then anything inside the pane that names the terminal would be obeyed.
    # Sending the project's name means every redraw corrects it instead.
    for name, line in enabling:
        assert '"on"' in line, (name, line)

    strings = [
        (name, line)
        for name, source in sources.items()
        for line in source.splitlines()
        if '"set-titles-string"' in line
    ]
    assert len(strings) == 2, strings
    for name, line in strings:
        assert "slot" not in line and "label" not in line and "role" not in line, (name, line)
    # And the value each of them sends is the project's, by name.
    for name, source in sources.items():
        block = source.split('"set-titles-string"', 1)[1][:200]
        assert "title" in block, (name, block)


def _konsole_titles(layout: Path, *, settle_seconds: float = 30.0) -> list[str]:
    return _konsole_reading(layout, settle_seconds=settle_seconds)[0]


def _konsole_reading(
    layout: Path,
    *,
    settle_seconds: float = 30.0,
    config_dir: Path | None = None,
    config_home: Path | None = None,
    attempts: int = 3,
) -> tuple[list[str], list[str]]:
    """Read a window, retrying only a reading that says nothing either way.

    Offscreen, Konsole sometimes comes up never honouring the window-title
    escape at all: every caption stays the profile default, which names the
    working directory and the whole command line. That is not this suite's
    subject failing -- it is a window that was never told -- and it happens
    often enough on a loaded host to have failed a correct candidate in Audit.

    A reading like that is retried from a fresh window. A reading that says
    something is returned as it is, right or wrong: neither the project name
    nor a pane title names a path, so a caption that has been captured by a
    pane still fails on the first attempt.
    """
    for attempt in range(attempts):
        titles, windows = _konsole_reading_once(
            layout, settle_seconds=settle_seconds, config_dir=config_dir, config_home=config_home
        )
        if not any("/" in title for title in windows):
            return titles, windows
    return titles, windows


def _konsole_reading_once(
    layout: Path,
    *,
    settle_seconds: float = 30.0,
    config_dir: Path | None = None,
    config_home: Path | None = None,
) -> tuple[list[str], list[str]]:
    """What a real Konsole shows: every split's title, and the window's own.

    The window titles are read one per focus change, because that is the
    reported fault -- the caption was correct until a pane was clicked. Focus
    is moved over Konsole's own interface rather than by clicking, which is the
    same thing as far as the caption is concerned: it follows the active
    session either way.
    """
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
            return [], []
        environment = dict(os.environ)
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["DBUS_SESSION_BUS_ADDRESS"] = address
        if config_dir is not None:
            # Exactly what a launched window is given, and nothing else on the
            # search path: the default has to be doing the work here.
            environment["XDG_CONFIG_DIRS"] = str(config_dir)
        # A configuration directory of its own, so this never reads or writes
        # the Konsole profiles of whoever is running the suite. A case that
        # supplies one is modelling a desktop account that already has
        # preferences of its own.
        home = config_home if config_home is not None else layout.parent / "konsole-config"
        home.mkdir(parents=True, exist_ok=True)
        environment["XDG_CONFIG_HOME"] = str(home)

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
            return [], []
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
        def window_title() -> str:
            match = re.search(
                r'variant\s+string "(.*)"',
                ask(f"--dest={service}", "/konsole/MainWindow_1",
                    "org.freedesktop.DBus.Properties.Get",
                    "string:org.qtproject.Qt.QWidget", "string:windowTitle"),
            )
            return match.group(1) if match else ""

        window_titles: list[str] = []
        for index in range(1, 7):
            ask(f"--dest={service}", "/Windows/1",
                "org.kde.konsole.Window.setCurrentSession", f"int32:{index}")
            # Waited for, not slept at. Until a split's own escapes have been
            # processed, the caption is Konsole's profile default -- the
            # working directory and the whole command line -- and reading it
            # then made this case fail while the behaviour was correct. That
            # fallback is recognisable by what is in it: it names a path. No
            # title this suite is about does, whether right (`Switchyard`) or
            # wrong (`syrd slot 1: director`), so waiting for one that does not
            # waits out the not-yet without waiting out a regression. Whatever
            # was last seen is returned if it never settles, so a case that
            # times out fails on what it actually read (SYRD-141 audit).
            deadline = time.time() + settle_seconds
            seen = window_title()
            while time.time() < deadline:
                if seen and "/" not in seen and GENERIC_TITLE_MARKER not in seen:
                    settled = window_title()
                    if settled == seen:
                        break
                    seen = settled
                    continue
                time.sleep(0.4)
                seen = window_title()
            window_titles.append(seen)
        return titles, window_titles
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
    expected = EXPECTED_HEADERS
    assert titles == expected, titles
    assert not any("Switchyard" in title for title in titles), titles
    # The live regression, stated as the thing that must not come back.
    assert not any(GENERIC_TITLE_MARKER in title for title in titles), titles
    assert not any("switchyard-pane" in title for title in titles), titles


def _konsole_fixture(tmp_path: Path, *, project_name: str = "Switchyard") -> Path:
    """The real wrapper, an inert client, and the generated layout."""
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    shutil.copy2(
        ROOT / "scripts" / team_launcher.PANE_WINDOW_NAME,
        launcher_dir / team_launcher.PANE_WINDOW_NAME,
    )
    stub = launcher_dir / "team-launcher"
    stub.write_text("#!/bin/sh\nexec sleep 900\n", encoding="utf-8")
    stub.chmod(0o755)
    config, config_path = _project(tmp_path, project_name=project_name)
    leaves = _materialize(tmp_path, config, config_path, script_path=stub)
    assert len(leaves) == 6, leaves
    return tmp_path / "out.json"


def test_a_real_konsole_keeps_the_window_title_through_every_focus_change() -> None:
    """The reported regression, against a real Konsole, one focus at a time.

    The User's report was that focusing a pane renamed the whole window. This
    moves focus through all six splits and reads the window's own title after
    each move, so it fails if the caption ever follows a split again -- whether
    because the wrapper stops reporting the window title, because the cascaded
    default stops reaching Konsole, or because a future Konsole changes which
    of the two it shows.
    """
    if not all(shutil.which(program) for program in ("konsole", "dbus-send", "dbus-daemon")):
        return
    with tempfile.TemporaryDirectory(prefix="pgu-presentation-window.") as tmp:
        tmp_path = Path(tmp)
        layout = _konsole_fixture(tmp_path)
        # The product's own writer, not a copy of its contents here: if the
        # default it writes stops being the one Konsole needs, this fails.
        defaults_dir = tmp_path / "konsole-defaults"
        assert team_launcher.write_konsole_config_defaults(defaults_dir, model=layout) == ""
        splits, windows = _konsole_reading(layout, config_dir=defaults_dir)

    if not splits or not any(splits):
        return
    assert splits == EXPECTED_HEADERS, splits
    assert windows == ["Switchyard"] * 6, windows


def test_without_the_default_konsole_does_exactly_what_was_reported() -> None:
    """Proof that the default is what holds the title, not something else.

    The same layout, the same wrapper, the same escapes -- and no cascaded
    Konsole default. The caption follows the focused split, which is the fault
    as the User saw it. Without this the passing case above could be passing
    for a reason nobody has identified.
    """
    if not all(shutil.which(program) for program in ("konsole", "dbus-send", "dbus-daemon")):
        return
    with tempfile.TemporaryDirectory(prefix="pgu-presentation-nodefault.") as tmp:
        tmp_path = Path(tmp)
        layout = _konsole_fixture(tmp_path)
        splits, windows = _konsole_reading(layout, config_dir=tmp_path / "empty")

    if not splits or not any(splits):
        return
    assert windows == splits, (windows, splits)
    assert len(set(windows)) == 6, windows


def test_the_launch_puts_the_konsole_default_where_konsole_reads_it() -> None:
    """The default is written beside the layout and named on the search path.

    Beside it because the account that can read one can read the other; on
    `XDG_CONFIG_DIRS` because that is where KConfig looks for a value the user
    has not set, and it stops answering the moment they set one themselves.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-konsole-defaults.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text("{}\n", encoding="utf-8")
        layout.chmod(0o600)
        assert team_launcher.write_konsole_config_defaults(tmp_path, model=layout) == ""
        written = tmp_path / team_launcher.KONSOLE_DEFAULTS_NAME
        assert written.read_text(encoding="utf-8") == team_launcher.KONSOLE_WINDOW_TITLE_DEFAULTS
        # Whoever can read the layout can read this, because that is the
        # account the terminal runs as.
        assert stat.S_IMODE(written.stat().st_mode) == stat.S_IMODE(layout.stat().st_mode)

        # A real local account, because an unresolvable one refuses before it
        # builds anything and this case would then assert nothing at all.
        me = pwd.getpwuid(os.getuid()).pw_name
        args = [
            str(value)
            for value in team_launcher.konsole_launch_args(
                layout, gui_user=me, window_title="Switchyard", config_dir=tmp_path
            )
        ]
        assert args[:2] != ["sh", "-lc"], args
        assert any(
            value.startswith("XDG_CONFIG_DIRS=") and value.split("=", 1)[1].split(":")[0] == str(tmp_path)
            for value in args
        ), args
        assert "--qwindowtitle" in args and "Switchyard" in args, args


def test_the_launch_itself_names_the_directory_it_just_wrote() -> None:
    """The decision `launch_konsole_window` makes, not the one it delegates.

    `konsole_launch_args` will put any directory it is given on the search
    path; what matters live is that the launch gives it the one it just wrote
    the default into. A launch that writes the file and then forgets to name it
    leaves Konsole reading nothing and the window title following focus again,
    with every other case here still passing (SYRD-139).
    """
    with tempfile.TemporaryDirectory(prefix="pgu-konsole-launch.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text("{}\n", encoding="utf-8")
        launched: list[list[str]] = []

        class _Started:
            pid = 4242

            def poll(self):
                return None

        original = team_launcher.default_gui_user
        try:
            team_launcher.default_gui_user = lambda: pwd.getpwuid(os.getuid()).pw_name
            status = team_launcher.launch_konsole_window(
                layout,
                project="porter",
                window_title="Switchyard",
                gui_user=pwd.getpwuid(os.getuid()).pw_name,
                process_launcher=lambda args, **kwargs: launched.append([str(a) for a in args]) or _Started(),
            )
        finally:
            team_launcher.default_gui_user = original

        assert status == 0, status
        assert launched, "the launch built no command"
        args = launched[0]
        named = [value for value in args if value.startswith("XDG_CONFIG_DIRS=")]
        assert named, args
        assert named[0].split("=", 1)[1].split(":")[0] == str(tmp_path), named
        # And the file it points at is really there, with the setting in it.
        written = tmp_path / team_launcher.KONSOLE_DEFAULTS_NAME
        assert written.read_text(encoding="utf-8") == team_launcher.KONSOLE_WINDOW_TITLE_DEFAULTS


def _slot_sessions(server: str, config) -> list[str]:
    """Six display slots, configured by the product's own tmux options.

    The options are not retyped here: `display_slot_terminal_title_commands`
    is what the presentation runs against a live slot, and running exactly
    that argv is what makes a mutation of it fail this case.
    """
    from scripts import presentation_controller

    sessions = []
    for slot in range(6):
        session = f"syrd141-slot-{slot}"
        _run_isolated_tmux(
            server,
            ["new-session", "-d", "-s", session, "-x", "80", "-y", "24", "sh", "-c", "exec sleep 900"],
            check=True,
        )
        _run_isolated_tmux(server, ["set-option", "-t", f"{session}:", "status", "off"], check=True)
        for args in presentation_controller.display_slot_terminal_title_commands(config, session):
            assert args[0] == "tmux", args
            _run_isolated_tmux(server, list(args[1:]), check=True)
        sessions.append(session)
    return sessions


def test_the_slot_title_command_names_the_tenant_not_the_slot() -> None:
    """The argv itself, for two tenants and every slot.

    Data-driven both ways: a tenant with a display name sends that, a tenant
    without one sends its slug, and neither sends anything that varies by slot
    or role -- which is what made the caption move when focus did.
    """
    from scripts import presentation_controller

    for project, project_name, expected in (
        ("porter", "Switchyard", "Switchyard"),
        ("otto", "Otto Works", "Otto Works"),
        ("otto", "", "otto"),
    ):
        with tempfile.TemporaryDirectory(prefix="pgu-slot-title.") as tmp:
            config, _ = _project(Path(tmp), project=project, project_name=project_name)
            sent = []
            for slot in range(6):
                commands = presentation_controller.display_slot_terminal_title_commands(
                    config, f"{project}-display-{slot}"
                )
                assert [args[-2] for args in commands] == ["set-titles", "set-titles-string"], commands
                assert commands[0][-1] == "on", commands[0]
                sent.append(commands[1][-1])
        assert sent == [expected] * 6, (project, project_name, sent)


def test_a_focused_tmux_pane_cannot_replace_the_project_caption() -> None:
    """The live regression: the caption read `syrd slot 1: director`.

    SYRD-139's Konsole case passed while the window was still wrong, because
    its panes ran an inert stub and never started tmux. A display slot sets
    `set-titles on` with a string naming the slot and the role, so the moment
    its client attached it overwrote the window title the pane wrapper had just
    set -- and Konsole's caption, which shows that title, followed the focused
    pane.

    So this runs tmux. Six isolated display slots carrying the product's own
    title options, the real wrapper, the real cascaded Konsole default, and a
    desktop account whose own konsolerc says the opposite -- then focus moves
    across all six splits and the window title is read after each move. The
    only thing standing in for production is which tmux socket the panes attach
    to, which is the isolation every other case in this suite uses.
    """
    if not all(shutil.which(program) for program in ("konsole", "dbus-send", "dbus-daemon", "tmux")):
        return
    server = f"syrd141-{os.getpid()}"
    _cleanup_dead_isolated_tmux_socket(server)
    with tempfile.TemporaryDirectory(prefix="pgu-presentation-tmux.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path = _project(tmp_path, project_name="Switchyard")
        wrapper = tmp_path / "bin" / team_launcher.PANE_WINDOW_NAME
        wrapper.parent.mkdir()
        shutil.copy2(ROOT / "scripts" / team_launcher.PANE_WINDOW_NAME, wrapper)
        try:
            sessions = _slot_sessions(server, config)
            layout = _six_pane_layout(tmp_path / "layout.json")
            document = json.loads(layout.read_text(encoding="utf-8"))
            for slot, leaf in enumerate(team_launcher._layout_leaves(document)):
                # The product's command builder, so the escapes under test are
                # the ones a real pane is given.
                leaf["Command"] = team_launcher.inert_pane_command(
                    wrapper,
                    ["tmux", "-L", server, "attach", "-t", sessions[slot]],
                    title=team_launcher.pane_split_title(config, config.roles[slot]),
                    window_title=team_launcher.project_window_title(config),
                )
                leaf["WorkingDirectory"] = str(tmp_path)
            layout.write_text(json.dumps(document) + "\n", encoding="utf-8")

            # The desktop account already prefers the other behaviour. The
            # presentation has to win for its own window without editing this.
            user_config = tmp_path / "konsole-config"
            user_config.mkdir()
            (user_config / "konsolerc").write_text(
                "[KonsoleWindow]\nShowWindowTitleOnTitleBar=false\n", encoding="utf-8"
            )
            defaults = tmp_path / "konsole-defaults"
            assert team_launcher.write_konsole_config_defaults(defaults, model=layout) == ""

            splits, windows = _konsole_reading(
                layout, config_dir=defaults, config_home=user_config
            )
            # Their preference is still theirs. Konsole rewrites its own
            # configuration on exit, so what is asserted is the thing this
            # change promises: the value it overrode for this window is not
            # changed in the file it overrode it from.
            after = (user_config / "konsolerc").read_text(encoding="utf-8")
            assert "ShowWindowTitleOnTitleBar=false" in after, after
            assert "[$i]" not in after, after
        finally:
            _run_isolated_tmux(server, ["kill-server"], check=False, capture_output=True)

    if not splits or not any(splits):
        return
    assert splits == EXPECTED_HEADERS, splits
    assert windows == ["Switchyard"] * 6, windows
    # The exact shape the User reported, named so a regression says so.
    assert not any("slot " in window for window in windows), windows


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_presentation_titles_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
