"""The Wayland displays the presentation-flow suites name are fixtures, not live sockets (SYRD-621).

Since SYRD-621 every display a window opens on -- an explicit override
included -- must be a live socket the desktop account owns in its own runtime
directory. These suites are about what a launch does with a display, not about
which display is live: they name `wayland-0`, or `HOST_WAYLAND_DISPLAY` paths
like `/run/user/1000/wayland-0`, for accounts that are not real and runtimes
that do not exist. Importing this names that default and judges a display by
its shape alone -- `wayland-*` in a runtime directory under `/run/user` --
never by the filesystem, so the host running a suite cannot change its answer.
Whose runtime it is, and whether the socket is live, real and the account's,
are the dedicated suite's questions.

This is a test seam and lives only here. Selection itself, with the real
socket, owner and link checks, is `tests/wayland_display_selection_test.py`,
which does not import it.
"""

from __future__ import annotations

import os
from pathlib import Path

from scripts import wayland_selection

os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-0"


def fixture_socket_problem(path: Path, *, uid: int) -> str:
    if path.parent.parent != wayland_selection.RUN_USER_ROOT or not path.name.startswith("wayland-"):
        return f"{path} is not a Wayland socket in {wayland_selection.runtime_dir(uid)}"
    return ""


wayland_selection.socket_problem = fixture_socket_problem
