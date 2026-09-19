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

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import presentation_controller as presentation  # noqa: E402
from scripts import team_launcher  # noqa: E402

#: What the Zorin viewer window is, and what the defect was reported at.
WIDTH = team_launcher.DEFAULT_VIEWER_COLUMNS
HEIGHT = team_launcher.DEFAULT_VIEWER_ROWS

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


class Server:
    """A private tmux server, so nothing here can reach a live tenant.

    Isolated by `TMUX_TMPDIR` rather than by `-L`, because a viewer pane's own
    command re-invokes tmux to attach and carries no socket flag: it has to
    land on the same server, and only the directory reaches it.
    """

    def __init__(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="syrd216-tmux.")
        self.env = {**os.environ, "TMUX_TMPDIR": self.tmpdir}

    def tmux(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, env=self.env, **kwargs
        )

    def runner(self, args, **kwargs):
        """Stand in for subprocess.run, so the real code builds the real argv.

        Everything the caller asked for -- capture, text, devnull -- is passed
        through, because the production code varies those per call and a
        fixture that overrode them would be testing its own argv, not the
        code's.
        """
        kwargs.setdefault("capture_output", "stdout" not in kwargs and "stderr" not in kwargs)
        kwargs.setdefault("text", True)
        return subprocess.run(args, env=self.env, **kwargs)

    def kill(self) -> None:
        self.tmux("kill-server")
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
            server.tmux(
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
                server.tmux("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT),
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
        server.tmux("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT), "-s", "v", "sleep 600")
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
            server.tmux("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT),
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
            grid = team_launcher.viewer_grid(empty)
        except ValueError:
            check(True, "an empty viewer is refused")
        else:
            raise AssertionError(f"viewer_grid({empty}) returned {grid}")
    for panes in range(1, 13):
        columns, rows = team_launcher.viewer_grid(panes)
        check(columns * rows >= panes, f"{panes} panes do not fit {columns}x{rows}")
        check(columns >= rows, f"{panes} panes come out {rows} deep and only {columns} across")
        check((columns - 1) * rows < panes,
              f"{panes} panes leave a whole unused column in {columns}x{rows}")


def test_tmux_tiled_is_the_shape_this_replaces() -> None:
    """The defect itself, pinned: `tiled` really does give five panes 2x3.

    Without this the suite above could pass against a tmux whose `tiled` was
    already 3x2, and would then be asserting nothing about the change.
    """
    server = Server()
    try:
        server.tmux("new-session", "-d", "-x", str(WIDTH), "-y", str(HEIGHT), "-s", "t", "sleep 600")
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
