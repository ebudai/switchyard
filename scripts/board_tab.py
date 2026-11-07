"""Opening a project's board in a browser tab once an explicit launch has brought it up (SYRD-680).

An operator who ran `switchyard new` or `switchyard <slug>` used to have to ask for the board's address. Now
the command that owns the screen opens it, once, after the board answers:

- **Which address.** `board_url` takes the port root's own unit names
  (`board_identity.unit_board_address`), because the tenant can rewrite its configuration and this opens a page
  in a human's browser. A configuration's `board_url` is the fallback for a board with no root unit, and only
  when it is loopback. Either way the URL is rebuilt from its scheme, host and port alone, so no path, query or
  credential is carried.
- **When.** `wait_for_board` asks `/api/build`, a bounded answer, until the board answers or the bound passes.
  A board that never answers is said, with its address, and opens nothing. It never fails the launch.
- **Where.** In the desktop session the presentation window uses: `presentation_gui_user` for a configuration,
  the bridge's caller otherwise, reached through `_gui_launch_prefix`, as the terminal is. A project without
  desktop access, a host with no session to reach, or no `xdg-open` prints the address instead.
- **How often.** `present_project_board` is called only by the explicit commands, never by `launch_project`,
  which upgrades, cutovers and recoveries also run. Within one process it acts once per project. The owner
  half of a bridged launch does nothing, because its caller owns the screen and opens the tab (SYRD-211).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

#: How long a launch waits for its board to answer before it says so and goes on without a tab.
BOARD_READY_TIMEOUT_SECONDS = 30.0
BOARD_READY_POLL_SECONDS = 1.0
#: The board's bounded "which build" answer (SYRD-703). Never `/api/client-config`, which carries the write token.
READY_PATH = "api/build"
READY_ANSWER_LIMIT_BYTES = 4 << 10
#: How long `xdg-open` is given to hand the URL to the browser. One that is still running after it is left to run:
#: on some desktops it is the browser itself.
OPENER_WAIT_SECONDS = 10.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: Projects this process has already presented, so one explicit command acts at most once per project.
_PRESENTED: set[str] = set()


def board_url(project: str, *, config: Any = None,
              address: Callable[[str], tuple[int, str]] | None = None) -> tuple[str, str]:
    """`(url, "")` for this project's board, or `("", why)`."""
    from scripts.ticket_board import board_identity

    try:
        port, _prefix = (address or board_identity.unit_board_address)(project)
    except (board_identity.BoardIdentityError, OSError) as exc:
        unit_problem = str(exc)
    else:
        return f"http://127.0.0.1:{port}/", ""
    configured = str(getattr(config, "board_url", "") or "").strip()
    if not configured:
        return "", unit_problem
    try:
        parts = urllib.parse.urlsplit(configured)
        port = parts.port
    except ValueError:
        return "", f"its configured board_url {configured!r} is not a URL"
    if parts.scheme not in ("http", "https") or parts.hostname not in LOOPBACK_HOSTS or port is None:
        return "", (f"root's unit names no board ({unit_problem}), and its configured board_url is not a "
                    "loopback http address with a port")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}:{port}/", ""


def wait_for_board(url: str, *, timeout: float = BOARD_READY_TIMEOUT_SECONDS,
                   poll: float = BOARD_READY_POLL_SECONDS,
                   opener: Callable[..., Any] = urllib.request.urlopen,
                   clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> bool:
    """Whether the board at `url` answers HTTP within `timeout`. Any HTTP answer counts: it is up."""
    deadline = clock() + timeout
    while True:
        try:
            with opener(url + READY_PATH, timeout=min(3.0, max(timeout, 0.1))) as response:
                response.read(READY_ANSWER_LIMIT_BYTES)
            return True
        except urllib.error.HTTPError:
            return True
        except (OSError, ValueError):
            pass
        if clock() >= deadline:
            return False
        sleep(poll)


def board_tab_plan(project: str, *, config: Any = None, gui_user: str = "", desktop: bool | None = None,
                   address: Callable[[str], tuple[int, str]] | None = None) -> dict[str, Any]:
    """What `present_project_board` would do, changing nothing: for a dry run."""
    url, problem = board_url(project, config=config, address=address)
    if not url:
        return {"url": None, "action": f"none: the board's address is not known ({problem})"}
    if not (_desktop(config) if desktop is None else desktop):
        return {"url": url, "action": "print the address: the project has no desktop access"}
    user = gui_user or _gui_user(config)
    return {"url": url, "action": f"open one tab in {user or 'the desktop'}'s browser with xdg-open once the board "
                                  f"answers, within {BOARD_READY_TIMEOUT_SECONDS:.0f} s"}


def present_project_board(
    project: str,
    *,
    config: Any = None,
    gui_user: str = "",
    desktop: bool | None = None,
    dry_run: bool = False,
    address: Callable[[str], tuple[int, str]] | None = None,
    ready: Callable[[str], bool] = wait_for_board,
    popen: Callable[..., Any] = subprocess.Popen,
    which: Callable[..., str | None] = shutil.which,
    geteuid: Callable[[], int] = os.geteuid,
    print_func: Callable[[str], None] = print,
) -> str:
    """Open the project's board once, in the desktop session that owns its presentation, or print its address.

    Never raises for a board or desktop problem and never fails the launch: what happened is said, and the
    answer -- "opened", "printed", "skipped" or "dry-run" -- is returned for the caller's tests.
    """
    from scripts import team_launcher as launcher

    if launcher.running_through_tenant_control():
        return "skipped"  # the bridge's caller owns the screen, and opens it (SYRD-211)
    if project in _PRESENTED:
        return "skipped"
    if dry_run:
        plan = board_tab_plan(project, config=config, gui_user=gui_user, desktop=desktop, address=address)
        print_func(f"switchyard: dry run: {project}'s board {plan['url'] or ''}: would {plan['action']}; "
                   "nothing was opened")
        return "dry-run"
    _PRESENTED.add(project)
    url, problem = board_url(project, config=config, address=address)
    if not url:
        print_func(f"switchyard: {project}'s board address is not known, so no tab was opened: {problem}")
        return "printed"
    if not (_desktop(config) if desktop is None else desktop):
        print_func(f"switchyard: {project}'s board: {url}")
        return "printed"
    if not ready(url):
        print_func(f"switchyard: {project}'s board did not answer at {url} within "
                   f"{BOARD_READY_TIMEOUT_SECONDS:.0f} s, so no tab was opened; open it once "
                   f"`switchyard status {project}` shows the board up")
        return "printed"
    user = gui_user or _gui_user(config)
    if geteuid() != 0 and user and user != launcher.current_user_name():
        # Another account's desktop is reached only by crossing from root, as the window is (SYRD-43).
        print_func(f"switchyard: {project}'s board: {url} (no tab was opened: this account cannot reach "
                   f"{user}'s desktop)")
        return "printed"
    prefix, refusal = launcher._gui_launch_prefix(gui_user=user)
    opener = which("xdg-open", path=launcher.DEFAULT_PANE_BASE_PATH) or which("xdg-open")
    if refusal or not opener:
        print_func(f"switchyard: {project}'s board: {url} (no tab was opened: "
                   f"{' '.join(refusal.split()) if refusal else 'xdg-open is not installed'})")
        return "printed"
    try:
        child = popen([*prefix, opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        print_func(f"switchyard: {project}'s board: {url} (no tab was opened: {exc})")
        return "printed"
    try:
        code = child.wait(timeout=OPENER_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        code = 0  # still handing it over, or it is the browser: either way it is opening
    if code != 0:
        print_func(f"switchyard: {project}'s board: {url} (no tab was opened: xdg-open exited {code})")
        return "printed"
    print_func(f"switchyard: opened {project}'s board in {user or 'the desktop'}'s browser: {url}")
    return "opened"


def _desktop(config: Any) -> bool:
    """Whether this project presents on a desktop: its recorded policy grants one."""
    policy = getattr(config, "desktop_access", None)
    return isinstance(policy, Mapping) and policy.get("mode") == "wayland"


def _gui_user(config: Any) -> str:
    from scripts import team_launcher as launcher

    return launcher.presentation_gui_user(config) if config is not None else launcher.default_gui_user()
