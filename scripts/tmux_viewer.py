"""The tmux viewer: the argv that builds it, its layout, its re-layout hooks, and launching it.

A tenant's viewer is one tmux session that shows every role's pane as a
split, sized and titled for the person watching:
- **Session options:** `tmux_set_*_args` and
  `configure_tmux_session_options` apply them to a role's own session
  (status off, mouse, `DEFAULT_TMUX_HISTORY_LIMIT` of history, no border
  status).
- **The viewer's argv:** `tmux_viewer_*_args` create it, pin and unpin its
  size (`DEFAULT_VIEWER_COLUMNS` by `DEFAULT_VIEWER_ROWS`), split it, and
  set its status, titles, prefix, borders and per-pane role.
- **Layout:** `viewer_grid` and `viewer_layout_string` compute the tiled
  layout tmux accepts, including its checksum (`_tmux_layout_checksum`,
  `_tmux_layout_spans`, `VIEWER_CELL_ASPECT`).
- **Re-layout hooks:** `install_viewer_relayout_hook`,
  `tmux_viewer_relayout_hook_args`, `tmux_viewer_observer_hook_args` and
  `viewer_layout_helper_path` keep the layout right when the window resizes.
- **The launch:** `launch_tmux_viewer_session`.

Session existence and kill argv and command quoting stay in
`scripts/team_launcher.py`. This module reads them from there when a function
runs. Session records and the rest of the session lifecycle are not here.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-309). `team_launcher`
imports this module at its top and still exports every name callers read there
(`presentation_controller` and `switchyard-viewer-layout` among them). This
module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import math
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig


DEFAULT_VIEWER_COLUMNS = 240


DEFAULT_VIEWER_ROWS = 80


DEFAULT_TMUX_HISTORY_LIMIT = 200_000


def tmux_set_status_off_args(role: RoleConfig) -> list[str]:
    return ["tmux", "set-option", "-t", role.tmux_session, "status", "off"]


def tmux_set_mouse_args(session: str) -> list[str]:
    return ["tmux", "set-option", "-t", session, "mouse", "on"]


def tmux_set_history_limit_args(session: str) -> list[str]:
    return ["tmux", "set-option", "-t", session, "history-limit", str(DEFAULT_TMUX_HISTORY_LIMIT)]


def configure_tmux_session_options(
    session: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    for args in (tmux_set_mouse_args(session), tmux_set_history_limit_args(session)):
        proc = runner(args)
        if proc.returncode != 0:
            return int(proc.returncode)
    return 0


def tmux_set_pane_border_status_off_args(role: RoleConfig) -> list[str]:
    return ["tmux", "set-window-option", "-t", f"{role.tmux_session}:0", "pane-border-status", "off"]


def tmux_viewer_new_session_args(viewer_session: str, role: RoleConfig) -> list[str]:
    from scripts import team_launcher as launcher

    command = launcher._quote_command(["env", "TMUX=", "tmux", "attach", "-t", role.tmux_session])
    return [
        "tmux",
        "new-session",
        "-d",
        "-x",
        str(DEFAULT_VIEWER_COLUMNS),
        "-y",
        str(DEFAULT_VIEWER_ROWS),
        "-s",
        viewer_session,
        "-c",
        role.workdir,
        command,
    ]


def tmux_viewer_pin_size_args(viewer_session: str) -> list[str]:
    """Hold the viewer window at its working size while it is being built.

    `new-session -d -x 240 -y 80` is not enough on its own. tmux 3.2a -- what
    Zorin and every Ubuntu 22.04 derivative ship -- ignores that size for a
    session with no client and makes the window its default, 80x23. Six panes
    are then split into 23 rows by halving the newest one, and the fourth split
    fails with "no space for new pane": test11's viewer never finished and no
    window opened (SYRD-221 UAT). tmux 3.7 honours the flags, which is why
    nothing here saw it. Measured on both.

    `resize-window` sets the size on both, and pins it: the window's
    `window-size` becomes `manual`. That pin is released by
    `tmux_viewer_unpin_size_args` once the viewer is built.
    """
    return [
        "tmux", "resize-window", "-t", f"{viewer_session}:0",
        "-x", str(DEFAULT_VIEWER_COLUMNS), "-y", str(DEFAULT_VIEWER_ROWS),
    ]


def tmux_viewer_unpin_size_args(viewer_session: str) -> list[str]:
    """Give the built viewer back to whoever opens a window on it.

    Left pinned, the viewer stays 240x80 whatever the person's window is, and
    the relayout hook never fires (SYRD-216). Unset, the window keeps its size
    until a client attaches and then follows that client -- measured on 3.2a
    and 3.7c: 240x80 with no client, 120x39 for a 120x40 window, 200x59 after
    it is enlarged.
    """
    return ["tmux", "set-option", "-w", "-u", "-t", f"{viewer_session}:0", "window-size"]


def tmux_viewer_split_window_args(viewer_session: str, role: RoleConfig) -> list[str]:
    from scripts import team_launcher as launcher

    command = launcher._quote_command(["env", "TMUX=", "tmux", "attach", "-t", role.tmux_session])
    return ["tmux", "split-window", "-t", f"{viewer_session}:0", "-c", role.workdir, command]


def _tmux_layout_checksum(layout: str) -> int:
    """tmux's own 16-bit layout checksum, which it refuses a layout without."""
    value = 0
    for character in layout:
        value = (value >> 1) + ((value & 1) << 15)
        value = (value + ord(character)) & 0xFFFF
    return value


def _tmux_layout_spans(total: int, parts: int) -> list[int]:
    """`parts` sizes across `total` cells, one cell per divider between them.

    The remainder goes to the leftmost or topmost, which is what tmux does and
    is why a five-pane bottom row reads as two equal halves rather than one
    short one.
    """
    base, extra = divmod(total - (parts - 1), parts)
    return [base + (1 if index < extra else 0) for index in range(parts)]


#: A terminal cell is about twice as tall as it is wide, so a window is only
#: really wider than it is tall once it has twice as many columns as rows.
VIEWER_CELL_ASPECT = 2


def viewer_grid(panes: int, *, width: int, height: int) -> tuple[int, int]:
    """Columns and rows for `panes` viewer panes in a `width` x `height` window.

    tmux's own `tiled` grows rows before columns and never looks at the window,
    so five panes come out two columns by three rows whatever shape the window
    is: on a wide one, every pane unnecessarily narrow and the shape of an empty
    sixth cell where the last row is short.

    The counts come from the integer square root either way; which of them is
    the column count is the window's business. Five panes are three across and
    two down on a landscape window, and two across and three down on a portrait
    one -- the same grid, turned to match (SYRD-216).
    """
    if panes < 1:
        raise ValueError("a viewer needs at least one pane")
    across = max(1, math.isqrt(panes))
    along = -(-panes // across)
    if width >= height * VIEWER_CELL_ASPECT:
        return (along, across)
    return (across, along)


def viewer_layout_string(panes: int, *, width: int, height: int) -> str:
    """An explicit tmux layout for `panes` panes in a `width` x `height` window.

    Written out rather than asked for by name because tmux has no named layout
    with this shape. Panes are filled row-major in pane order, so slot order
    reads left to right and then down, and a short last row spreads across the
    whole width instead of leaving a gap.
    """
    columns, _ = viewer_grid(panes, width=width, height=height)
    per_row = [min(columns, panes - start) for start in range(0, panes, columns)]
    heights = _tmux_layout_spans(height, len(per_row))
    rows: list[str] = []
    top = 0
    pane = 0
    for count, row_height in zip(per_row, heights):
        cells = []
        left = 0
        for cell_width in _tmux_layout_spans(width, count):
            cells.append(f"{cell_width}x{row_height},{left},{top},{pane}")
            left += cell_width + 1
            pane += 1
        if count > 1:
            rows.append(f"{width}x{row_height},0,{top}" + "{" + ",".join(cells) + "}")
        else:
            # A row holding one pane IS that pane: there is nothing for a
            # side-by-side container to arrange, and tmux writes none either.
            rows.append(f"{width}x{row_height},0,{top},{pane - 1}")
        top += row_height + 1
    # The stack around the rows is written even when there is one of them:
    # tmux reads a container holding a single child exactly as it reads the
    # child, so there is no case to split here.
    body = f"{width}x{height},0,0[" + ",".join(rows) + "]"
    return f"{_tmux_layout_checksum(body):04x},{body}"


def viewer_layout_helper_path() -> Path:
    """The re-layout helper, in whichever release this code is running from."""
    return Path(__file__).resolve().parent / "switchyard-viewer-layout"


def tmux_viewer_relayout_hook_args(viewer_session: str) -> list[str]:
    """Keep the layout matching the window's shape, not just its first shape.

    tmux scales a layout on resize and never re-derives it, so without this a
    viewer laid out for a wide window keeps that topology when the desktop
    window is dragged tall and narrow. `window-resized` is the hook that
    carries the new size; `client-resized` fires before the window has followed
    and would apply the previous shape's layout (SYRD-216).

    The socket is passed explicitly rather than left to the ambient `TMUX`,
    because a hook's command inherits the server's environment and not a
    client's.
    """
    from scripts import team_launcher as launcher

    helper = launcher._quote_command(
        [sys.executable, str(viewer_layout_helper_path()), f"{viewer_session}:0",
         "--socket", "#{socket_path}"]
    )
    # `-w` is kept although a `session:window` target already scopes the hook
    # to the window on this tmux -- measured, with and without, on a session
    # holding two windows. It says which kind of hook this is, and it does not
    # rest on undocumented behaviour holding in another version.
    return [
        "tmux", "set-hook", "-w", "-t", f"{viewer_session}:0",
        "window-resized", f"run-shell {shlex.quote(helper)}",
    ]


def tmux_viewer_observer_hook_args(viewer_session: str, event: str, *, index: int) -> list[str]:
    """Re-decide the viewer's observers whenever a client comes or goes.

    Global, because a window opened on a slot is a client of the SLOT's session,
    not the viewer's, and a session hook on the viewer would never hear of it.
    Indexed per project so tenants sharing a server keep their own. Run in the
    background: a hook that blocks holds up the attach it is reacting to.
    """
    from scripts import team_launcher as launcher

    helper = launcher._quote_command(
        [sys.executable, str(viewer_layout_helper_path()), f"{viewer_session}:0",
         "--observers", "--socket", "#{socket_path}"]
    )
    return ["tmux", "set-hook", "-g", f"{event}[{index}]", f"run-shell -b {shlex.quote(helper)}"]


#: Said when this tmux cannot re-derive the viewer's layout on resize. tmux 3.3
#: added `window-resized`; 3.2a -- Zorin, Ubuntu 22.04 -- rejects it as an
#: invalid option, and treating that as fatal failed the whole viewer after its
#: six panes were built (SYRD-221 UAT, found building against a real 3.2a).
VIEWER_RELAYOUT_UNAVAILABLE_NOTE = (
    "switchyard: this tmux has no window-resized hook (added in tmux 3.3), so the "
    "viewer's layout scales with its window rather than being re-derived for a new shape"
)


def install_viewer_relayout_hook(
    viewer_session: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] | None = None,
) -> int:
    """Install the SYRD-216 relayout hook, or say plainly why this tmux cannot.

    Only an unknown hook is forgiven. It is not replaced with `client-resized`,
    which fires before the window has followed and would lay out the previous
    shape (SYRD-216): no hook is better than a wrong one. Any other failure is
    still a failure.
    """
    say = print_func or (lambda line: print(line, file=sys.stderr))
    proc = runner(tmux_viewer_relayout_hook_args(viewer_session), capture_output=True, text=True)
    if proc.returncode == 0:
        return 0
    detail = str(getattr(proc, "stderr", "") or "")
    if "invalid option" in detail and "window-resized" in detail:
        say(VIEWER_RELAYOUT_UNAVAILABLE_NOTE)
        return 0
    if detail.strip():
        say(detail.strip())
    return int(proc.returncode) or 1


def tmux_viewer_select_layout_args(viewer_session: str, panes: int) -> list[str]:
    return [
        "tmux",
        "select-layout",
        "-t",
        f"{viewer_session}:0",
        viewer_layout_string(panes, width=DEFAULT_VIEWER_COLUMNS, height=DEFAULT_VIEWER_ROWS),
    ]


def tmux_viewer_set_status_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-option", "-t", viewer_session, "status", "on"]


def tmux_viewer_set_titles_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-option", "-t", viewer_session, "set-titles", "on"]


def tmux_viewer_set_titles_string_args(viewer_session: str, title: str) -> list[str]:
    return ["tmux", "set-option", "-t", viewer_session, "set-titles-string", title]


def tmux_viewer_set_prefix_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-option", "-t", viewer_session, "prefix", "C-a"]


def tmux_viewer_set_border_status_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-window-option", "-t", f"{viewer_session}:0", "pane-border-status", "top"]


def tmux_viewer_set_border_format_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-window-option", "-t", f"{viewer_session}:0", "pane-border-format", " #{@role} "]


def tmux_viewer_set_role_arg(viewer_session: str, pane_index: int, role: str) -> list[str]:
    return ["tmux", "set-option", "-p", "-t", f"{viewer_session}.{pane_index}", "@role", role]


def launch_tmux_viewer_session(
    roles: Sequence[RoleConfig],
    *,
    viewer_session: str,
    window_title: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    from scripts import team_launcher as launcher

    if not roles:
        print("team-launcher: no visible roles for viewer layout", file=sys.stderr)
        return 1
    if runner(launcher.tmux_has_session_by_name_args(viewer_session), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        kill_proc = runner(launcher.tmux_kill_session_by_name_args(viewer_session))
        if kill_proc.returncode != 0:
            return int(kill_proc.returncode)
    for role in roles:
        for args in (tmux_set_status_off_args(role), tmux_set_pane_border_status_off_args(role)):
            proc = runner(args)
            if proc.returncode != 0:
                return int(proc.returncode)
    first, *rest = roles
    proc = runner(tmux_viewer_new_session_args(viewer_session, first))
    if proc.returncode != 0:
        return int(proc.returncode)
    proc = runner(tmux_viewer_pin_size_args(viewer_session))
    if proc.returncode != 0:
        return int(proc.returncode)
    configure_result = configure_tmux_session_options(viewer_session, runner=runner)
    if configure_result != 0:
        return configure_result
    for role in rest:
        proc = runner(tmux_viewer_split_window_args(viewer_session, role))
        if proc.returncode != 0:
            return int(proc.returncode)
    proc = runner(tmux_viewer_select_layout_args(viewer_session, len(roles)))
    if proc.returncode != 0:
        return int(proc.returncode)
    hooked = install_viewer_relayout_hook(viewer_session, runner=runner)
    if hooked != 0:
        return hooked
    for args in (
        tmux_viewer_set_status_args(viewer_session),
        tmux_viewer_set_titles_args(viewer_session),
        tmux_viewer_set_titles_string_args(viewer_session, window_title or viewer_session),
        tmux_viewer_set_prefix_args(viewer_session),
        tmux_viewer_set_border_status_args(viewer_session),
        tmux_viewer_set_border_format_args(viewer_session),
        tmux_viewer_unpin_size_args(viewer_session),
    ):
        proc = runner(args)
        if proc.returncode != 0:
            return int(proc.returncode)
    for index, role in enumerate(roles):
        proc = runner(tmux_viewer_set_role_arg(viewer_session, index, role.role))
        if proc.returncode != 0:
            return int(proc.returncode)
    return 0
