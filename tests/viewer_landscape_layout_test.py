#!/usr/bin/env python3
"""SYRD-216 live UAT: five roles came up two columns by three rows.

tmux's own `tiled` grows rows before columns, so five panes land as 2x3 -- every
pane unnecessarily narrow, and the last row short, leaving the visual shape of
an empty sixth cell. On the wide window a desktop viewer always gets that is
backwards. Switchyard's presentation is three columns across the top and the
remainder across the full width below.

These cases drive a REAL tmux server and read the geometry tmux actually
produced. A test that counted panes, or compared the argv to the same function
that built it, would have reported this correct while it was wrong: the pane
count never changed, and the shape is the whole defect.
"""

from __future__ import annotations

import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import presentation_controller as presentation  # noqa: E402
from scripts import team_launcher  # noqa: E402

#: What the Zorin viewer window is, and what the defect was reported at.
WIDTH = team_launcher.DEFAULT_VIEWER_COLUMNS
HEIGHT = team_launcher.DEFAULT_VIEWER_ROWS
#: A window turned on its side, which is what the kickback was about.
TALL_WIDTH = HEIGHT
TALL_HEIGHT = WIDTH

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


class Server:
    """A private tmux server, so nothing here can reach a live tenant.

    The boundary is an explicit socket path, chosen by this fixture before it
    runs anything. `-S` on every command it issues means there is no server it
    could reach but its own, whatever the environment says -- which matters
    because the teardown kills servers and the first version of this class
    proved its provenance only *after* creating a session on whatever it found.

    `TMUX_TMPDIR` is set to the same directory as well, and `TMUX` stripped,
    because the code under test re-invokes tmux without a socket flag: a viewer
    pane's attach command and the resize hook's helper both have to land on
    this same server, and only the environment reaches them. A tmux command
    with `TMUX` set and no `-L`/`-S` talks to the server that variable names
    and ignores `TMUX_TMPDIR` entirely -- run from inside a tmux pane, an
    earlier version of this suite built its sessions on the caller's own server
    and ended by killing it, which it did to a live desktop repeatedly.
    """

    def __init__(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="syrd216-tmux.")).resolve()
        # Where TMUX_TMPDIR would put the default socket, named explicitly so
        # every command can be pinned to it and the two agree by construction.
        self.socket = self.tmpdir / f"tmux-{os.getuid()}" / "default"
        # tmux makes this directory itself when it derives the path from
        # TMUX_TMPDIR, but not when handed one with `-S`.
        self.socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["TMUX_TMPDIR"] = str(self.tmpdir)
        # Assigned, not defaulted. `tmux attach` refuses to start on a terminal
        # it cannot drive -- this role's pane inherits `TERM=dumb`, and under
        # it the client dies with "terminal does not support clear" before it
        # ever attaches. `setdefault` kept that value and the suite then waited
        # thirty seconds for an attachment that could never happen.
        self.env["TERM"] = "xterm-256color"
        #: Every argv this fixture issued, so a test can assert what it did NOT.
        self.issued: list[list[str]] = []

    def _within(self, candidate: str | Path) -> bool:
        """Containment by path components, not by string prefix.

        `/tmp/syrd216-tmux.ab` is not inside `/tmp/syrd216-tmux.a`, however the
        two read as strings.
        """
        try:
            return Path(candidate).resolve().is_relative_to(self.tmpdir)
        except (OSError, ValueError):
            return False

    def boundary(self) -> None:
        """Refuse before touching anything, not after."""
        if not self._within(self.socket):
            raise AssertionError(
                f"refusing to run: the socket {self.socket} is outside "
                f"{self.tmpdir}, so this fixture would be operating on a "
                "server it does not own -- and its teardown kills servers"
            )

    def socket_path(self) -> str | None:
        """The socket of the server actually running, or None if there is none."""
        probe = self.tmux("display-message", "-p", "#{socket_path}")
        if probe.returncode != 0:
            return None
        return probe.stdout.strip() or None

    def verify(self) -> str:
        """Prove the *running* server is ours. Raises when it is not."""
        socket = self.socket_path()
        if socket is None:
            raise AssertionError(
                f"no tmux server resolved for {self.tmpdir}; refusing to treat "
                "an absent answer as proof of anything"
            )
        if not self._within(socket):
            raise AssertionError(
                f"refusing to continue: tmux resolved to {socket}, outside "
                f"{self.tmpdir}"
            )
        return socket

    def start(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Create the first session, having first proved where it will land."""
        self.boundary()
        created = self.tmux(*args)
        self.verify()
        return created

    def tmux(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = ["tmux", "-S", str(self.socket), *args]
        self.issued.append(argv)
        return subprocess.run(argv, capture_output=True, text=True, env=self.env, **kwargs)

    def runner(self, args, **kwargs):
        """Stand in for subprocess.run, so the real code builds the real argv.

        No `-S` is added here, deliberately: this is the argv under test, and a
        fixture that rewrote it would be testing its own. The environment is
        what keeps it on this server, exactly as it keeps the pane commands
        that argv creates on it.
        """
        self.issued.append(list(args))
        kwargs.setdefault("capture_output", "stdout" not in kwargs and "stderr" not in kwargs)
        kwargs.setdefault("text", True)
        return subprocess.run(args, env=self.env, **kwargs)

    def kill(self) -> None:
        """Tear down, having first proved which server would be torn down."""
        try:
            self.boundary()
            socket = self.socket_path()
            if socket is None:
                return          # nothing running: nothing to kill, and no guess
            if not self._within(socket):
                raise AssertionError(
                    f"refusing to kill {socket}: it is outside {self.tmpdir} and "
                    "is therefore somebody else's server"
                )
            self.tmux("kill-server")
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def geometry(self, session: str) -> list[tuple[int, int, int, int, int]]:
        out = self.tmux(
            "list-panes", "-t", f"{session}:0", "-F",
            "#{pane_index} #{pane_left} #{pane_top} #{pane_width} #{pane_height}",
        ).stdout
        return sorted(
            tuple(int(part) for part in line.split())
            for line in out.splitlines()
            if line.strip()
        )


def client_said(master: int) -> str:
    """Whatever the client printed to its terminal before dying."""
    chunks = []
    while True:
        try:
            data = os.read(master, 4096)
        except (BlockingIOError, OSError):
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode("utf-8", "replace").strip()


def wait_for_attachment(
    server: "Server", session: str, child: int, master: int, timeout: float = 30.0,
) -> None:
    """Wait for a real client to be attached, and say so if it never can be.

    A client that has already exited is never going to attach, and waiting the
    whole timeout to report that hides the reason. What it printed is the
    reason -- under a terminal tmux cannot drive it is "open terminal failed",
    which says nothing about layouts and would otherwise look like the layout
    hook failing to fire.
    """
    os.set_blocking(master, False)
    deadline = time.monotonic() + timeout
    attached = "0"
    while time.monotonic() < deadline:
        finished, status = os.waitpid(child, os.WNOHANG)
        if finished:
            raise AssertionError(
                f"the viewer client exited before attaching (wait status "
                f"{status}); its terminal said: {client_said(master)!r}"
            )
        attached = server.tmux(
            "display-message", "-p", "-t", session, "#{session_attached}"
        ).stdout.strip()
        if attached not in ("", "0"):
            return
        time.sleep(0.2)
    raise AssertionError(
        f"no client attached within {timeout:g}s (session_attached={attached!r}); "
        f"its terminal said: {client_said(master)!r}"
    )


def wait_for_window(
    server: "Server", session: str, columns: int, rows: int,
    want: list[int], what: str, timeout: float = 30.0,
) -> None:
    """Wait for the window to BE that size and to have that shape.

    Both together, and that is the whole point. Waiting on topology alone
    returns the instant it is already right, and a shape that was never reached
    by this resize is no evidence that the resize did anything: the first step
    of the transition case wants `[3,2]`, which is exactly what the
    creation-time layout already is, so the loop finished before the client had
    attached and the window was still its creation size. That is the race this
    suite was kicked back for.
    """
    want_window = f"{columns}x{rows - 1}"        # one row goes to the status line
    deadline = time.monotonic() + timeout
    seen = None
    while time.monotonic() < deadline:
        window = server.tmux(
            "display-message", "-p", "-t", f"{session}:0",
            "#{window_width}x#{window_height}",
        ).stdout.strip()
        shape = [len(row) for row in rows_of(server.geometry(session))]
        seen = (window, shape)
        if seen == (want_window, want):
            return
        time.sleep(0.2)
    raise AssertionError(
        f"{what}: waited {timeout:g}s for window {want_window} shape {want}; "
        f"last saw window {seen[0] if seen else None} shape {seen[1] if seen else None}"
    )


def rows_of(geometry) -> list[list[tuple[int, int, int, int, int]]]:
    """Panes grouped by their top edge, in reading order."""
    rows: dict[int, list] = {}
    for pane in geometry:
        rows.setdefault(pane[2], []).append(pane)
    return [sorted(rows[top], key=lambda p: p[1]) for top in sorted(rows)]


def assert_fills_width(row, width: int, what: str) -> None:
    """Adjacent panes, one divider between each, and no gap at either end."""
    check(row[0][1] == 0, f"{what}: the row does not start at the left edge")
    edge = 0
    for pane in row:
        check(pane[1] == edge, f"{what}: pane {pane[0]} is not flush with the one before it")
        edge = pane[1] + pane[3] + 1
    check(edge - 1 == width, f"{what}: the row leaves {width - (edge - 1)} columns unused")
    # Widest first, which is what tmux does with a spare column of its own, so
    # the same viewer looks the same whichever path laid it out. Not "within
    # one column of each other": tmux's own rescaling on a resize spreads the
    # remainder more widely than that, and that is its business, not ours.
    widths = [pane[3] for pane in row]
    check(widths == sorted(widths, reverse=True),
          f"{what}: widths {widths} are not widest-first, so the spare column moved")


def assert_fills_height(rows, height: int, what: str) -> None:
    """Rows stacked with one line between them, and nothing left over.

    Compared against content coordinates: the viewer draws its slot labels on
    the pane borders, so the first row's content starts one line down and each
    divider line carries the next row's label. A row's own border is therefore
    part of the line that separates it from the row above.
    """
    top_row = rows[0][0]
    check(top_row[2] <= 1, f"{what}: the top row starts {top_row[2]} lines down")
    previous = top_row
    for row in rows[1:]:
        expected = previous[2] + previous[4] + 1
        check(row[0][2] == expected,
              f"{what}: a row starts at {row[0][2]} rather than {expected}, leaving a gap")
        previous = row[0]
    check(previous[2] + previous[4] == height,
          f"{what}: the bottom row ends at {previous[2] + previous[4]}, not {height}")


def config_for(tmp: Path, project: str) -> team_launcher.ProjectConfig:
    return team_launcher.ProjectConfig(
        project=project,
        project_name="Switchyard",
        ticket_prefix="SYRD",
        layout=tmp / "layout.json",
        session_dir=tmp / "sessions",
        board_url="http://127.0.0.1:0",
        board_socket=str(tmp / "board.sock"),
        upstream_report_url="",
        upstream_report_token_file="",
        run_as_user="switchyard-agent",
        pane_launcher=None,
        repository=tmp / "repo",
        control_repository=tmp / "repo.git",
        worktree_base=tmp / "worktrees",
        worktree_remote="origin",
        worktree_branch="main",
        roles=[],
    )


def test_the_viewer_builder_lays_out_the_slots_it_was_given() -> None:
    """The reported defect, through the code that builds the real viewer.

    Five is what Zorin reported. Six is what the live project runs, and it is
    here so that a viewer laid out for a hardcoded count cannot pass.
    """
    for slots, shape in ((5, [3, 2]), (6, [3, 3])):
        _drive_viewer(slots, shape)


def _drive_viewer(slots: int, shape: list[int]) -> None:
    server = Server()
    tmp = Path(tempfile.mkdtemp(prefix="syrd216-cfg."))
    project = f"syrd216x{slots}"
    try:
        for slot in range(slots):
            server.start(
                "new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT),
                "-s", presentation.display_session_name(project, slot), "sleep 600",
            )
        config = config_for(tmp, project)
        state = {"slot_count": slots, "slots": {str(s): f"role{s}" for s in range(slots)}}
        presentation._launch_viewer(config, state, runner=server.runner)

        viewer = team_launcher.viewer_session_for_project(project)
        geometry = server.geometry(viewer)
        check(len(geometry) == slots, f"the viewer has {len(geometry)} panes, not {slots}")
        rows = rows_of(geometry)
        check([len(row) for row in rows] == shape,
              f"{slots} roles came out as rows of {[len(row) for row in rows]}, not {shape}")
        # Not a placeholder in sight: every row spans the whole window...
        for index, row in enumerate(rows):
            assert_fills_width(row, WIDTH, f"{slots} roles, row {index}")
        # ...and the rows between them cover the whole height.
        assert_fills_height(rows, HEIGHT, f"{slots} roles")
        # Role order reads left to right, then down -- slot 0 top-left through
        # the last bottom-right -- and the labels are on the panes to match.
        check([pane[0] for pane in [p for row in rows for p in row]] == list(range(slots)),
              "pane order does not read left to right and then down")
        for slot in range(slots):
            label = server.tmux(
                "show-options", "-p", "-v", "-t", f"{viewer}:0.{slot}", "@switchyard_role"
            ).stdout.strip()
            check(label == f"role{slot}", f"slot {slot} is labelled {label!r}")
        # A short last row spreads instead of leaving a gap: with five slots the
        # bottom two take half the window each rather than a third.
        if len(rows[-1]) < len(rows[0]):
            check(min(pane[3] for pane in rows[-1]) > max(pane[3] for pane in rows[0]),
                  "the short row is no wider per pane than a full one, so it did not spread")
    finally:
        server.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _role(index: int, tmp: Path) -> team_launcher.RoleConfig:
    return team_launcher.RoleConfig(
        role=f"role{index}", slot=index, detached=False,
        tmux_session=f"syrd216-role{index}", target=f"syrd216-role{index}:0.0",
        workdir=str(tmp), cli=["sleep"], model="", model_arg="", effort="",
        yolo=False, extra_args=[], resume_mode="", resume_flag="",
        resume_subcommand="", fresh_session_per_ticket=False,
        live_commands=["sleep"], env={},
    )


def test_the_launcher_lays_out_the_roles_it_was_given() -> None:
    """The other viewer builder, and the count it passes.

    `launch_tmux_viewer_session` is the direct-attach path rather than the
    display-slot one. It is driven here with a recording runner, and the
    `select-layout` it emits is then applied to a real tmux window of that many
    panes -- so the assertion is about the geometry that argv produces, not
    about the argv matching the function that built it.
    """
    tmp = Path(tempfile.mkdtemp(prefix="syrd216-roles."))
    try:
        for count, shape in ((3, [3]), (5, [3, 2]), (6, [3, 3])):
            issued: list[list[str]] = []

            def record(args, **kwargs):
                issued.append(list(args))
                return subprocess.CompletedProcess(args, 0, "", "")

            team_launcher.launch_tmux_viewer_session(
                [_role(index, tmp) for index in range(count)],
                viewer_session="syrd216-viewer",
                runner=record,
            )
            layouts = [a for a in issued if a[:2] == ["tmux", "select-layout"]]
            check(len(layouts) == 1, f"{count} roles issued {len(layouts)} select-layout calls")
            check(layouts[0][-1] != "tiled", f"{count} roles still ask tmux for its own tiled")

            server = Server()
            try:
                server.start("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT),
                            "-s", "v", "sleep 600")
                for _ in range(count - 1):
                    server.tmux("split-window", "-t", "v:0", "sleep 600")
                    server.tmux("select-layout", "-t", "v:0", "tiled")
                applied = server.tmux("select-layout", "-t", "v:0", layouts[0][-1])
                check(applied.returncode == 0,
                      f"tmux refused what the launcher issued for {count}: {applied.stderr.strip()}")
                rows = rows_of(server.geometry("v"))
                check([len(row) for row in rows] == shape,
                      f"the launcher's layout for {count} gave {[len(row) for row in rows]}, not {shape}")
            finally:
                server.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_shape_survives_the_window_being_resized() -> None:
    """A viewer is resized by whatever client attaches to it."""
    server = Server()
    try:
        server.start("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT), "-s", "v", "sleep 600")
        for _ in range(4):
            server.tmux("split-window", "-t", "v:0", "sleep 600")
        applied = server.tmux(*team_launcher.tmux_viewer_select_layout_args("v", 5)[1:])
        check(applied.returncode == 0, f"tmux refused the layout: {applied.stderr.strip()}")
        for width, height in ((180, 50), (320, 100), (WIDTH, HEIGHT)):
            server.tmux("resize-window", "-t", "v:0", "-x", str(width), "-y", str(height))
            rows = rows_of(server.geometry("v"))
            check([len(row) for row in rows] == [3, 2],
                  f"at {width}x{height} the rows became {[len(row) for row in rows]}")
            for index, row in enumerate(rows):
                assert_fills_width(row, width, f"{width}x{height} row {index}")
            assert_fills_height(rows, height, f"{width}x{height}")
    finally:
        server.kill()


def test_every_viewer_size_keeps_its_grid() -> None:
    """Fewer roles and six roles are laid out too, and none of them regress.

    One through six because that is every viewer there can be: a role's slot is
    0-5, so six panes is the most the window ever holds.
    """
    expected = {1: [1], 2: [2], 3: [3], 4: [2, 2], 5: [3, 2], 6: [3, 3]}
    server = Server()
    try:
        for panes, shape in expected.items():
            session = f"v{panes}"
            server.start("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT),
                        "-s", session, "sleep 600")
            for _ in range(panes - 1):
                server.tmux("split-window", "-t", f"{session}:0", "sleep 600")
                # Fixture only: redistribute so the next split has room. The
                # layout under test is the one applied below.
                server.tmux("select-layout", "-t", f"{session}:0", "tiled")
            applied = server.tmux(*team_launcher.tmux_viewer_select_layout_args(session, panes)[1:])
            check(applied.returncode == 0,
                  f"tmux refused the {panes}-pane layout: {applied.stderr.strip()}")
            rows = rows_of(server.geometry(session))
            check([len(row) for row in rows] == shape,
                  f"{panes} panes came out as {[len(row) for row in rows]}, not {shape}")
            for index, row in enumerate(rows):
                assert_fills_width(row, WIDTH, f"{panes} panes, row {index}")
            assert_fills_height(rows, HEIGHT, f"{panes} panes")
            # Never taller than wide: that is the whole point on a landscape
            # window, and it is what tmux's own tiled gets backwards.
            check(len(rows) <= len(rows[0]),
                  f"{panes} panes came out {len(rows)} rows deep and only {len(rows[0])} across")
    finally:
        server.kill()


def test_the_grid_rule_never_comes_out_taller_than_it_is_wide() -> None:
    """The rule itself, beyond the sizes a viewer can currently reach.

    Slots are capped at six today. If that cap ever moves, the shape should
    still be a landscape one rather than tmux's portrait default, so the rule
    is pinned here where no tmux server is needed to state it.
    """
    # A viewer with no panes is a caller's mistake, and it says so rather than
    # handing tmux an empty container to reject with something less useful.
    for empty in (0, -1):
        try:
            grid = team_launcher.viewer_grid(empty, width=240, height=80)
        except ValueError:
            check(True, "an empty viewer is refused")
        else:
            raise AssertionError(f"viewer_grid({empty}) returned {grid}")
    for panes in range(1, 13):
        columns, rows = team_launcher.viewer_grid(panes, width=240, height=80)
        check(columns * rows >= panes, f"{panes} panes do not fit {columns}x{rows}")
        check(columns >= rows, f"{panes} panes come out {rows} deep and only {columns} across")
        check((columns - 1) * rows < panes,
              f"{panes} panes leave a whole unused column in {columns}x{rows}")


def test_a_portrait_window_stacks_instead_of_squeezing() -> None:
    """The kickback: a tall narrow window must not keep the wide topology.

    Three columns in an 80-column window is 26 characters a pane. The same
    grid turned on its side is two columns and three rows, which is what tmux's
    own `tiled` happens to give here -- correct for this shape, and wrong for
    the one the ticket was filed about.
    """
    expected = {5: [2, 2, 1], 6: [2, 2, 2]}
    server = Server()
    try:
        for panes, shape in expected.items():
            session = f"p{panes}"
            server.start("new-session", "-d", "-x", str(TALL_WIDTH), "-y", str(TALL_HEIGHT),
                        "-s", session, "sleep 600")
            for _ in range(panes - 1):
                server.tmux("split-window", "-t", f"{session}:0", "sleep 600")
                server.tmux("select-layout", "-t", f"{session}:0", "tiled")
            layout = team_launcher.viewer_layout_string(
                panes, width=TALL_WIDTH, height=TALL_HEIGHT)
            applied = server.tmux("select-layout", "-t", f"{session}:0", layout)
            check(applied.returncode == 0,
                  f"tmux refused the portrait {panes}-pane layout: {applied.stderr.strip()}")
            rows = rows_of(server.geometry(session))
            check([len(row) for row in rows] == shape,
                  f"{panes} panes portrait came out {[len(row) for row in rows]}, not {shape}")
            for index, row in enumerate(rows):
                assert_fills_width(row, TALL_WIDTH, f"portrait {panes}, row {index}")
            assert_fills_height(rows, TALL_HEIGHT, f"portrait {panes}")
            # Turned, not squeezed: more rows than columns on a tall window.
            check(len(rows) >= len(rows[0]),
                  f"{panes} panes portrait is still {len(rows[0])} across and {len(rows)} down")
    finally:
        server.kill()


def test_more_columns_than_rows_is_not_the_same_as_wider_than_tall() -> None:
    """A cell is about twice as tall as it is wide, and the grid has to know.

    A 120x80 window has half again as many columns as rows and still shows as a
    portrait rectangle: 120 cells across at roughly half the width of a cell's
    height is narrower than 80 rows are tall. Comparing the counts alone calls
    it landscape and squeezes three columns into it. This is the only case that
    tells the two readings apart -- at 240x80 and 80x240 they agree.
    """
    for width, height, want in ((120, 80, [2, 2, 1]), (200, 80, [3, 2]), (160, 80, [3, 2])):
        columns, rows = team_launcher.viewer_grid(5, width=width, height=height)
        got = [min(columns, 5 - start) for start in range(0, 5, columns)]
        check(got == want, f"a {width}x{height} window gave rows of {got}, not {want}")
    server = Server()
    try:
        server.start("new-session", "-d", "-x", "120", "-y", "80", "-s", "n", "sleep 600")
        for _ in range(4):
            server.tmux("split-window", "-t", "n:0", "sleep 600")
            server.tmux("select-layout", "-t", "n:0", "tiled")
        applied = server.tmux("select-layout", "-t", "n:0",
                              team_launcher.viewer_layout_string(5, width=120, height=80))
        check(applied.returncode == 0, f"tmux refused it: {applied.stderr.strip()}")
        rows = rows_of(server.geometry("n"))
        check([len(row) for row in rows] == [2, 2, 1],
              f"a 120x80 window came out {[len(row) for row in rows]}, not [2, 2, 1]")
    finally:
        server.kill()


def test_the_arrangement_follows_the_window_through_a_real_resize() -> None:
    """The transition, driven by a real client changing shape under the viewer.

    Not `resize-window`: a desktop resizes a viewer by resizing the terminal
    its client is running in, and the hook that carries the new size only fires
    for that. The client needs a controlling terminal or the SIGWINCH a resize
    depends on is never delivered, and every hook looks dead -- which is how
    this was first mis-measured.
    """
    server = Server()
    child = None
    try:
        server.start("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT), "-s", "v", "sleep 600")
        for _ in range(4):
            server.tmux("split-window", "-t", "v:0", "sleep 600")
            server.tmux("select-layout", "-t", "v:0", "tiled")
        server.tmux(*team_launcher.tmux_viewer_select_layout_args("v", 5)[1:])
        installed = server.tmux(*team_launcher.tmux_viewer_relayout_hook_args("v")[1:])
        check(installed.returncode == 0, f"the hook was refused: {installed.stderr.strip()}")
        stored = server.tmux("show-hooks", "-w", "-t", "v:0").stdout
        check("window-resized" in stored, f"no window-resized hook on the viewer: {stored!r}")

        child, master = pty.fork()
        if child == 0:                                    # pragma: no cover - the client
            os.environ.clear()
            os.environ.update(server.env)
            os.execvp("tmux", ["tmux", "attach", "-t", "v"])

        def settle(columns: int, rows: int, want: list[int], what: str) -> None:
            wait_for_window(server, "v", columns, rows, want, what)

        # Attachment first, and waited for as attachment rather than inferred
        # from a shape that already matches. Until a client attaches the window
        # keeps the size it was created with, and every later assertion would
        # be about that rather than about a resize.
        # Size the client's terminal before waiting, so what attaches is a
        # 200x40 window rather than the pty's own default.
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
        wait_for_attachment(server, "v", child, master)
        settle(200, 40, [3, 2], "the client attaching at 200x40")

        for rows, columns, want in ((100, 60, [2, 2, 1]), (40, 200, [3, 2]), (100, 60, [2, 2, 1])):
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            settle(columns, rows, want, f"a {columns}x{rows} client")
            # The client keeps the window: a layout that fought it for the size
            # would show up here as a window that is not the client's shape.
            window = server.tmux("display-message", "-p", "-t", "v:0",
                                 "#{window_width}").stdout.strip()
            check(window == str(columns),
                  f"the window is {window} columns wide, not the client's {columns}")
    finally:
        if child:
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass
        server.kill()


def test_both_builders_install_the_hook_that_follows_the_window() -> None:
    """Neither viewer is left with a layout frozen at its first shape."""
    issued: list[list[str]] = []

    def record(args, **kwargs):
        issued.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    tmp = Path(tempfile.mkdtemp(prefix="syrd216-hook."))
    try:
        team_launcher.launch_tmux_viewer_session(
            [_role(index, tmp) for index in range(5)],
            viewer_session="syrd216-viewer", runner=record,
        )
        hooks = [a for a in issued if a[:2] == ["tmux", "set-hook"] and "window-resized" in a]
        check(len(hooks) == 1, f"the launcher installed {len(hooks)} resize hooks")
        check(str(team_launcher.viewer_layout_helper_path()) in " ".join(hooks[0]),
              f"the hook does not call the re-layout helper: {hooks[0]}")

        issued.clear()
        server = Server()
        try:
            project = "syrd216hook"
            for slot in range(5):
                server.start("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT),
                            "-s", presentation.display_session_name(project, slot), "sleep 600")
            presentation._launch_viewer(
                config_for(tmp, project),
                {"slot_count": 5, "slots": {str(s): f"role{s}" for s in range(5)}},
                runner=server.runner,
            )
            viewer = team_launcher.viewer_session_for_project(project)
            stored = server.tmux("show-hooks", "-w", "-t", f"{viewer}:0").stdout
            check("window-resized" in stored,
                  f"the display-slot viewer has no resize hook: {stored!r}")
        finally:
            server.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_client_that_cannot_start_is_reported_rather_than_waited_out() -> None:
    """The diagnosis, not just the timeout.

    This role's pane carries `TERM=dumb`, which tmux cannot drive: a client
    started under it dies immediately with "open terminal failed". The suite
    used to inherit that value, wait the full thirty seconds for an attachment
    that could never happen, and report a layout timeout -- which is a true
    statement about the wrong thing, and sent a reviewer looking at the hook.
    """
    server = Server()
    child = None
    try:
        server.start("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT),
                     "-s", "d", "sleep 600")
        doomed = dict(server.env)
        doomed["TERM"] = "dumb"
        child, master = pty.fork()
        if child == 0:                                    # pragma: no cover - the client
            os.environ.clear()
            os.environ.update(doomed)
            os.execvp("tmux", ["tmux", "attach", "-t", "d"])
        began = time.monotonic()
        try:
            wait_for_attachment(server, "d", child, master, timeout=20.0)
        except AssertionError as reported:
            elapsed = time.monotonic() - began
            check("exited before attaching" in str(reported),
                  f"the report does not say the client died: {reported}")
            check("open terminal failed" in str(reported),
                  f"the client's own words are missing from the report: {reported}")
            check(elapsed < 10,
                  f"took {elapsed:.1f}s to notice a client that had already exited")
        else:
            raise AssertionError("a client that cannot start was reported as attached")
    finally:
        if child:
            try:
                os.kill(child, signal.SIGTERM)
            except (ProcessLookupError, ChildProcessError):
                pass
        server.kill()


def test_the_transition_wait_does_not_accept_a_shape_it_already_had() -> None:
    """The race itself, tested rather than only fixed.

    A window laid out `[3,2]` at 240x80 with nobody attached will never become
    200x39. Waiting on the shape alone returns immediately and reports success;
    waiting on both has to time out. Without this the suite could drift back to
    the version that was kicked back and stay green.
    """
    server = Server()
    try:
        server.start("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT),
                     "-s", "w", "sleep 600")
        for _ in range(4):
            server.tmux("split-window", "-t", "w:0", "sleep 600")
            server.tmux("select-layout", "-t", "w:0", "tiled")
        server.tmux(*team_launcher.tmux_viewer_select_layout_args("w", 5)[1:])
        check([len(row) for row in rows_of(server.geometry("w"))] == [3, 2],
              "the fixture window is not the shape this case depends on")
        try:
            wait_for_window(server, "w", 200, 40, [3, 2], "a window nothing will resize",
                            timeout=2.0)
        except AssertionError as timed_out:
            check("waited 2s" in str(timed_out), f"unexpected failure: {timed_out}")
        else:
            raise AssertionError(
                "the wait accepted a window that still had its creation size, "
                "because the shape happened to match already"
            )
    finally:
        server.kill()


def test_the_fixture_refuses_a_server_it_does_not_own_before_touching_it() -> None:
    """The guard has to bite before the first mutation, not after it.

    Two earlier versions of this were not that. The first asked
    `display-message` in `__init__`, before any server existed: tmux failed,
    the empty answer was read as "nothing unexpected", and it passed for the
    one reason it must never pass. The second proved provenance only after
    `new-session` had already run -- so a redirected fixture created a session
    on somebody else's server and only then refused. Given what this code did
    to a live desktop, the refusals are what deserve the tests.
    """
    # 1. Nothing running. `verify()` refuses rather than reading an absent
    #    answer as reassurance, and teardown kills nothing and guesses nothing.
    quiet = Server()
    try:
        quiet.verify()
    except AssertionError as refusal:
        check("no tmux server" in str(refusal), f"unexpected refusal: {refusal}")
    else:
        raise AssertionError("verify() accepted a fixture with no server running")
    quiet.kill()
    check(not any("kill-server" in args for args in quiet.issued),
          f"kill-server was issued with no server running: {quiet.issued}")

    # 2. A server that is somebody else's, standing in for the live one.
    bystander = Server()
    try:
        bystander.start("new-session", "-d", "-s", "not-ours", "sleep 600")
        before = bystander.tmux("list-sessions", "-F", "#{session_name}").stdout
        check("not-ours" in before, "the bystander server did not start")

        misdirected = Server()
        misdirected.socket = bystander.socket        # aimed at another server
        try:
            misdirected.start("new-session", "-d", "-s", "trespass", "sleep 600")
        except AssertionError as refusal:
            check("refusing to run" in str(refusal), f"unexpected refusal: {refusal}")
        else:
            raise AssertionError("start() created a session on another server")
        # Refused before the mutation, not after: nothing was even attempted.
        check(not any("new-session" in args for args in misdirected.issued),
              f"start() issued new-session before refusing: {misdirected.issued}")
        after = bystander.tmux("list-sessions", "-F", "#{session_name}").stdout
        check(after == before,
              f"the bystander's sessions changed: {before!r} -> {after!r}")

        # ...and its teardown refuses too, and kills nothing.
        try:
            misdirected.kill()
        except AssertionError as refusal:
            # The pre-check specifically: it refuses before it so much as asks
            # the other server a question, rather than probing and then
            # deciding.
            check("refusing to run" in str(refusal),
                  f"teardown refused, but only after probing: {refusal}")
        else:
            raise AssertionError("kill() accepted a server outside its own directory")
        check(not any("kill-server" in args for args in misdirected.issued),
              f"kill-server was issued at another server: {misdirected.issued}")
        check(bystander.socket_path() is not None,
              "the bystander server was killed by a fixture that does not own it")
        check("not-ours" in bystander.tmux("list-sessions", "-F", "#{session_name}").stdout,
              "the bystander lost its session")
    finally:
        bystander.kill()

    # 3. A sibling directory whose name merely starts the same way. String
    #    prefixes accept it; path components do not, and that is the check.
    collider = Server()
    try:
        sibling = Path(str(collider.tmpdir) + "-next-door")
        sibling.mkdir(parents=True, exist_ok=True)
        collider.socket = sibling / "default"
        check(str(collider.socket).startswith(str(collider.tmpdir)),
              "the collision fixture is not actually a prefix collision")
        try:
            collider.boundary()
        except AssertionError as refusal:
            check("refusing to run" in str(refusal), f"unexpected refusal: {refusal}")
        else:
            raise AssertionError(
                "a sibling sharing the directory-name prefix was accepted as inside"
            )
    finally:
        shutil.rmtree(str(collider.tmpdir) + "-next-door", ignore_errors=True)
        shutil.rmtree(collider.tmpdir, ignore_errors=True)


def test_tmux_tiled_is_the_shape_this_replaces() -> None:
    """The defect itself, pinned: `tiled` really does give five panes 2x3.

    Without this the suite above could pass against a tmux whose `tiled` was
    already 3x2, and would then be asserting nothing about the change.
    """
    server = Server()
    try:
        server.start("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT), "-s", "t", "sleep 600")
        for _ in range(4):
            server.tmux("split-window", "-t", "t:0", "sleep 600")
        server.tmux("select-layout", "-t", "t:0", "tiled")
        rows = rows_of(server.geometry("t"))
        check([len(row) for row in rows] == [2, 2, 1],
              f"tmux tiled gave {[len(row) for row in rows]}; this suite assumes it gives [2, 2, 1]")
    finally:
        server.kill()


def main() -> int:
    if not shutil.which("tmux"):
        print("viewer_landscape_layout_test: tmux unavailable")
        return 0
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"viewer_landscape_layout_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
