#!/usr/bin/env python3
"""SYRD-680: an explicit launch opens the project's board in a browser tab, once, after the board answers.

`switchyard new` used to finish without the board's address, and the operator asked Ops for it. Now the command
that owns the screen opens it (`scripts/board_tab.py`): the address root's unit names, after `/api/build` answers,
in the desktop the presentation window goes to, at most once per command. A headless project, a host it cannot
reach, a board that never answers and a missing `xdg-open` print the address instead and never fail the launch.

The browser is a stand-in throughout: `popen` records what would have run. No case here reads a live board or
reaches a desktop: every address comes from a stand-in unit, and every readiness answer from a stand-in opener.
"""

from __future__ import annotations

import http.server
import io
import os
import re
import subprocess
import sys
import threading
import urllib.error
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import board_tab, team_launcher  # noqa: E402

CHECKS = 0
PORT = 26680
URL = f"http://127.0.0.1:{PORT}/"
POLICY = {"mode": "wayland", "gui_user": "desktop-owner", "project": "p680", "tenant": "p680-agent"}


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def config(*, desktop: bool = True, board_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(project="p680", board_url=board_url, run_as_user="p680-agent", roles=[],
                           desktop_access=dict(POLICY) if desktop else {"mode": "headless"})


class Browser:
    """A stand-in for `xdg-open` behind the desktop prefix: records each argv, and exits as told."""

    def __init__(self, code: int | None = 0) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.code = code

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        code = self.code

        class Child:
            def wait(self, timeout=None):
                if code is None:
                    raise subprocess.TimeoutExpired(argv, timeout)
                return code
        return Child()


@contextmanager
def desktop(prefix=("env", "WAYLAND_DISPLAY=wayland-0"), refusal: str = "", me: str = "desktop-owner"):
    """This process's desktop: what `_gui_launch_prefix` answers, and who it is. Records who was asked for."""
    asked: list[str] = []

    def gui_prefix(*, gui_user=None, config_dir=None):
        asked.append(gui_user)
        return (list(prefix) if not refusal else []), refusal

    board_tab._PRESENTED.clear()
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(team_launcher, "_gui_launch_prefix", gui_prefix))
        stack.enter_context(mock.patch.object(team_launcher, "current_user_name", lambda: me))
        stack.enter_context(mock.patch.dict(os.environ, {team_launcher.TENANT_CONTROL_CALLER_ENV: ""}))
        yield asked
    board_tab._PRESENTED.clear()


def present(cfg=None, *, browser=None, ready=lambda _url: True, euid=1000, which=lambda *_a, **_k: "/usr/bin/xdg-open",
            **kwargs):
    said: list[str] = []
    answer = board_tab.present_project_board(
        "p680", config=cfg, address=lambda _p: (PORT, "P680"), ready=ready, popen=browser or Browser(),
        which=which, geteuid=lambda: euid, print_func=said.append, **kwargs)
    return answer, said


# ------------------------------------------------------------------ the address


def test_the_address_is_roots_unit_and_carries_nothing_else() -> None:
    url, problem = board_tab.board_url("p680", config=config(board_url="http://127.0.0.1:1/elsewhere"),
                                       address=lambda _p: (PORT, "P680"))
    check((url, problem) == (URL, ""), f"root's unit names the port, over the tenant's configuration: {url} {problem}")

    def no_unit(_project):
        raise board_tab_identity().BoardIdentityError("no unit")

    url, _ = board_tab.board_url("p680", config=config(board_url="http://ops:tok3n@localhost:18799/board?token=x#f"),
                                 address=no_unit)
    check(url == "http://localhost:18799/",
          f"without a unit, a loopback configured address is used, rebuilt from scheme, host and port only: {url}")
    for configured in ("http://evil.example:80/", "file:///etc/passwd", "http://127.0.0.1/", "not a url", ""):
        url, problem = board_tab.board_url("p680", config=config(board_url=configured), address=no_unit)
        check(url == "" and problem, f"{configured!r} is not opened: {problem}")


def board_tab_identity():
    from scripts.ticket_board import board_identity

    return board_identity


# ------------------------------------------------------------------ readiness


def test_readiness_is_bounded_and_asks_only_the_build() -> None:
    asked: list[str] = []
    now = [0.0]

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def opener(script):
        def open_(url, timeout):
            asked.append(url)
            outcome = script.pop(0) if script else "down"
            if outcome == "down":
                raise urllib.error.URLError("connection refused")
            if outcome == 404:
                raise urllib.error.HTTPError(url, 404, "not found", {}, None)
            return Answer(b'{"build_id": "x"}')
        return open_

    def sleep(seconds):
        now[0] += seconds

    ready = board_tab.wait_for_board(URL, timeout=30, poll=1, opener=opener(["down", "down", 200]),
                                     clock=lambda: now[0], sleep=sleep)
    check(ready and len(asked) == 3 and set(asked) == {URL + "api/build"},
          f"retries until the board answers, asking only its bounded build answer: {asked}")
    check(board_tab.wait_for_board(URL, timeout=30, poll=1, opener=opener([404]), clock=lambda: now[0], sleep=sleep),
          "an older board without /api/build still answered HTTP, so it is up")
    asked.clear()
    now[0] = 0.0
    check(not board_tab.wait_for_board(URL, timeout=5, poll=1, opener=opener([]), clock=lambda: now[0], sleep=sleep)
          and 5 <= len(asked) <= 7 and now[0] <= 6,
          f"a board that never answers is given up on at the bound: {len(asked)} asks, {now[0]} s")


def test_readiness_with_a_real_board_and_a_closed_port() -> None:
    class Board(http.server.BaseHTTPRequestHandler):
        paths: list[str] = []

        def do_GET(self):  # noqa: N802
            Board.paths.append(self.path)
            body = b'{"build_id": "abc"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        check(board_tab.wait_for_board(url, timeout=5) and Board.paths == ["/api/build"],
              f"a listening board is ready at once: {Board.paths}")
    finally:
        server.shutdown()
        server.server_close()
    check(not board_tab.wait_for_board(url, timeout=0.3, poll=0.1), "the same port closed is not ready")


# ------------------------------------------------------------------ opening it


def test_a_desktop_launch_opens_one_tab_in_the_presentation_desktop() -> None:
    with desktop() as asked:
        browser = Browser()
        answer, said = present(config(), browser=browser)
        check(answer == "opened" and browser.calls == [["env", "WAYLAND_DISPLAY=wayland-0", "/usr/bin/xdg-open", URL]]
              and browser.kwargs[0]["start_new_session"] is True and browser.kwargs[0]["stdin"] == subprocess.DEVNULL,
              f"one xdg-open of the board's address, through the desktop's own prefix, detached: {browser.calls}")
        check(asked == ["desktop-owner"],
              f"in the desktop the tenant's recorded policy names, as the window: {asked}")
        check(said == [f"switchyard: opened p680's board in desktop-owner's browser: {URL}"], f"and says so: {said}")
        again, said = present(config(), browser=browser)
        check(again == "skipped" and len(browser.calls) == 1 and said == [],
              f"the same command presenting again opens no second tab: {browser.calls}")


def test_root_crosses_to_the_desktop_account() -> None:
    with desktop(prefix=("sudo", "-u", "desktop-owner", "-H", "--", "env", "-i", "WAYLAND_DISPLAY=wayland-1"),
                 me="root") as asked:
        browser = Browser()
        answer, _said = present(config(), browser=browser, euid=0)
        check(answer == "opened" and browser.calls[0][:5] == ["sudo", "-u", "desktop-owner", "-H", "--"]
              and asked == ["desktop-owner"],
              f"a root launch (`switchyard new`) opens the tab as the desktop account, never as root: {browser.calls}")


def test_a_bridge_caller_opens_in_its_own_session() -> None:
    with desktop(me="eric") as asked:
        browser = Browser()
        answer, _said = present(None, browser=browser, gui_user="eric", desktop=True)
        check(answer == "opened" and asked == ["eric"] and browser.calls[0][-1] == URL,
              f"the bridge's caller, which cannot read the configuration, opens root's address in its own desktop: "
              f"{browser.calls}")


def test_where_no_tab_can_open_the_address_is_printed_and_the_launch_goes_on() -> None:
    cases = {
        "a headless project": dict(cfg=config(desktop=False)),
        "a project with no desktop policy": dict(cfg=SimpleNamespace(project="p680", board_url="", desktop_access=None)),
        "a host with no desktop session": dict(cfg=config(), refusal="no live Wayland socket for desktop-owner"),
        "no xdg-open": dict(cfg=config(), which=lambda *_a, **_k: None),
        "another account's desktop, unprivileged": dict(cfg=config(), me="p680-agent"),
        "xdg-open failing": dict(cfg=config(), code=3),
    }
    for name, case in cases.items():
        with desktop(refusal=case.get("refusal", ""), me=case.get("me", "desktop-owner")):
            browser = Browser(code=case.get("code", 0))
            answer, said = present(case["cfg"], browser=browser, **({"which": case["which"]} if "which" in case else {}))
            check(answer == "printed" and said and URL in said[-1] and "Traceback" not in "".join(said),
                  f"{name}: the address is printed: {said}")
            check(browser.calls == ([] if "code" not in case else browser.calls) and len(browser.calls) <= 1,
                  f"{name}: no tab, or the one failed attempt: {browser.calls}")
    with desktop():
        browser = Browser()
        answer, said = present(config(), browser=browser, ready=lambda _url: False)
        check(answer == "printed" and browser.calls == []
              and said == [f"switchyard: p680's board did not answer at {URL} within 30 s, so no tab was opened; "
                           "open it once `switchyard status p680` shows the board up"],
              f"a board that never answers opens nothing, and says what to do: {said}")


def test_a_slow_browser_is_left_to_open() -> None:
    with desktop():
        browser = Browser(code=None)
        answer, _said = present(config(), browser=browser)
        check(answer == "opened" and len(browser.calls) == 1,
              "an xdg-open still running after its wait is the browser starting, not a failure")


def test_the_owner_half_of_a_bridged_launch_opens_nothing() -> None:
    with desktop():
        browser = Browser()
        with mock.patch.dict(os.environ, {team_launcher.TENANT_CONTROL_CALLER_ENV: "eric"}):
            answer, said = present(config(), browser=browser)
        check(answer == "skipped" and browser.calls == [] and said == [],
              "the owner account has no screen; its caller opens the tab")


def test_a_dry_run_reports_and_changes_nothing() -> None:
    with desktop():
        browser = Browser()
        asked: list[str] = []
        answer, said = present(config(), browser=browser, dry_run=True, ready=lambda url: asked.append(url) or True)
        check(answer == "dry-run" and browser.calls == [] and asked == []
              and said == [f"switchyard: dry run: p680's board {URL}: would open one tab in desktop-owner's browser "
                           "with xdg-open once the board answers, within 30 s; nothing was opened"],
              f"the dry run names the address and the action, asks nothing and opens nothing: {said}")
        answer, _said = present(config(), browser=browser)
        check(answer == "opened" and len(browser.calls) == 1, "and does not use up the real launch's one tab")
        plan = board_tab.board_tab_plan("p680", config=config(desktop=False), address=lambda _p: (PORT, "P680"))
        check(plan == {"url": URL, "action": "print the address: the project has no desktop access"},
              f"a headless dry run says it would print: {plan}")


def test_the_real_path_end_to_end_with_a_stand_in_xdg_open() -> None:
    """No seam inside: a board on loopback, this account's own Wayland socket in a stand-in runtime directory, the
    launcher's real desktop prefix, and a real child -- an `xdg-open` that records what it was given."""
    import socket
    import tempfile

    from scripts import wayland_selection

    class Board(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"build_id": "abc"}'
            self.send_response(200 if self.path == "/api/build" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            return None

    me = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="b680.", dir="/tmp") as raw:
        tmp = Path(raw)
        runtime = tmp / "run" / str(os.getuid())
        runtime.mkdir(parents=True, mode=0o700)
        display = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        display.bind(str(runtime / "wayland-7"))
        display.listen(1)
        record = tmp / "opened"
        opener = tmp / "xdg-open"
        opener.write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$WAYLAND_DISPLAY" "$*" >> {record}\n')
        opener.chmod(0o755)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        cfg = SimpleNamespace(project="p680", board_url="", desktop_access={**POLICY, "gui_user": me})
        said: list[str] = []
        board_tab._PRESENTED.clear()
        try:
            env = {k: v for k, v in os.environ.items() if not k.endswith("WAYLAND_DISPLAY")}
            env[team_launcher.TENANT_CONTROL_CALLER_ENV] = ""
            with mock.patch.object(wayland_selection, "RUN_USER_ROOT", tmp / "run"), \
                    mock.patch.dict(os.environ, env, clear=True):
                for _ in range(2):
                    answer = board_tab.present_project_board(
                        "p680", config=cfg, address=lambda _p: (port, "P680"),
                        which=lambda *_a, **_k: str(opener), print_func=said.append)
                deadline = __import__("time").monotonic() + 10
                while not record.exists() and __import__("time").monotonic() < deadline:
                    __import__("time").sleep(0.05)
        finally:
            server.shutdown()
            server.server_close()
            display.close()
            board_tab._PRESENTED.clear()
        opened = record.read_text().splitlines() if record.exists() else []
        check(answer == "skipped" and opened == [f"{runtime / 'wayland-7'}|http://127.0.0.1:{port}/"],
              f"the real prefix opened the board once, on this account's own display: {opened} {said}")
        check(said == [f"switchyard: opened p680's board in {me}'s browser: http://127.0.0.1:{port}/"],
              f"and said so once: {said}")


# ------------------------------------------------------------------ who calls it


@contextmanager
def recorded_presents():
    calls: list[tuple[str, dict]] = []

    def record(project, **kwargs):
        calls.append((project, kwargs))
        return "opened"

    with mock.patch.object(team_launcher, "present_project_board", record):
        yield calls


def test_switchyard_slug_presents_after_a_successful_launch_only() -> None:
    from scripts import switchyard_dispatch

    cfg = config()
    for launched, expected in ((0, 1), (1, 0)):
        with recorded_presents() as calls, ExitStack() as stack:
            for name, value in {
                "report_installed_release_version": lambda: None,
                "_resolve_switchyard_project": lambda *_a, **_k: SimpleNamespace(slug="p680",
                                                                                 config_path=Path("/x/p680.json")),
                "_load_switchyard_project_config_for_command": lambda *_a, **_k: cfg,
                "resume_tenant": lambda *_a, **_k: [],
                "prepare_project_desktop": lambda c, **_k: c,
                "run_switchyard_launch_first_run_auth": lambda _c: None,
                "stop_before_launch_for_missing_owner_clis": lambda _r: False,
                "stop_before_launch_for_unauthenticated_providers": lambda _r: False,
                "stop_before_launch_for_unknown_models": lambda _r, **_k: False,
                "launch_project": lambda *_a, **_k: launched,
                "report_first_run_auth_warnings": lambda *_a, **_k: None,
            }.items():
                stack.enter_context(mock.patch.object(team_launcher, name, value))
            for argv in (["p680"], ["start", "p680"]):
                code = switchyard_dispatch.switchyard_main(argv)
                check(code == launched, f"{argv}: the launch's own answer is returned: {code}")
            check(len(calls) == 2 * expected and all(c == ("p680", {"config": cfg}) for c in calls),
                  f"launch {launched}: `switchyard p680` and `switchyard start p680` each present {expected}: {calls}")


def test_switchyard_new_presents_the_new_board_once() -> None:
    from scripts import new_project_phases

    for deferred in (False, True):
        with recorded_presents() as calls, ExitStack() as stack:
            launches: list[int] = []
            for name, value in {
                "ensure_staged_role_tooling": lambda *_a, **_k: [],
                "launch_project": lambda *_a, **_k: launches.append(1) or 0,
                "resolve_layout_mode": lambda *_a, **_k: team_launcher.LAYOUT_MODE_SEPARATE,
                "announce_new_project_presentation": lambda *_a, **_k: None,
                "report_first_run_auth_warnings": lambda *_a, **_k: None,
                "report_launch_session_records": lambda *_a, **_k: None,
                "_owner_state_layout_output_path": lambda *_a, **_k: Path("/x/layout.json"),
                "default_pane_state_dir_for_user": lambda *_a, **_k: Path("/x/panes"),
            }.items():
                stack.enter_context(mock.patch.object(team_launcher, name, value))
            said: list[str] = []
            code = new_project_phases._launch_new_project_panes(
                home_base=Path("/home"), pane_state_dir=None, session_record_timeout=0, session_record_poll=0,
                layout_mode="auto", layout_environ=None, konsole_process_launcher=None, print_func=said.append,
                include_designer=False, owner_user="p680-agent", resolved_slug="p680", runner=lambda *a, **k: None,
                stages=SimpleNamespace(begin=lambda _n: None, finish=lambda: None), config_path=Path("/x/p680.json"),
                report_summary="", config=config(), first_run_auth_report=None, launch_deferred=deferred,
                launch_runner=lambda *a, **k: None)
            expected = {"config": config(), "print_func": said.append} | ({"desktop": False} if deferred else {})
            check(code == 0 and len(calls) == 1 and calls[0][0] == "p680" and calls[0][1] == expected,
                  f"deferred={deferred}: the new board is presented once"
                  + (", by address, since no window opened" if deferred else "") + f": {calls}")
            check(len(launches) == (0 if deferred else 1), f"deferred={deferred}: around the one launch: {launches}")


def test_the_bridge_caller_presents_after_its_window_for_a_start_only() -> None:
    from scripts import command_crossing

    for operation, window, access, expected in (("start", 0, True, 1), ("start", 0, False, 1), ("start", 1, True, 0),
                                                ("stop", 0, True, 0), ("recover-display", 0, True, 0)):
        with recorded_presents() as calls, ExitStack() as stack:
            for name, value in {
                "current_user_name": lambda: "eric",
                "ensure_staged_role_bundle_before_crossing": lambda *_a, **_k: "",
                "complete_desktop_presentation": lambda *_a, **_k: window,
                "close_desktop_presentation": lambda *_a, **_k: 0,
                "_tenant_has_desktop_access": lambda *_a, access=access, **_k: access,
            }.items():
                stack.enter_context(mock.patch.object(team_launcher, name, value))
            try:
                command_crossing._switchyard_exec_through_tenant_control(
                    "p680", operation, grant={"authorized_user": "eric"},
                    runner=lambda *_a, **_k: SimpleNamespace(returncode=0), ensure_helper=lambda *_a, **_k: None,
                    print_func=lambda *_a: None)
            except SystemExit:
                pass
            check(len(calls) == expected and all(c[0] == "p680" and c[1]["gui_user"] == "eric" and c[1]["desktop"]
                                                 is access for c in calls),
                  f"{operation} with window {window}: the caller presents {expected}, with a tab only where the tenant "
                  f"has desktop access ({access}): {calls}")


def test_team_launcher_start_presents_and_its_dry_run_plans() -> None:
    from scripts import launcher_dispatch

    for dry_run, expected in ((False, 1), (True, 0)):
        with recorded_presents() as calls, ExitStack() as stack:
            cfg = config()
            stack.enter_context(mock.patch.object(team_launcher, "load_project_config", lambda *_a, **_k: cfg))
            stack.enter_context(mock.patch.object(team_launcher, "launch_project", lambda *_a, **_k: 0))
            argv = ["p680", "start", "--config", "/x/p680.json", *(["--dry-run"] if dry_run else [])]
            try:
                code = launcher_dispatch.main(argv)
            except SystemExit as exc:  # a parser or resolver this argv does not satisfy
                raise AssertionError(f"team-launcher {argv} did not reach the launch: {exc}") from None
            check(code == 0 and len(calls) == expected,
                  f"dry_run={dry_run}: an explicit start presents {expected}: {calls}")


def test_only_explicit_commands_present_the_board() -> None:
    """Upgrades, cutovers, recoveries and the listener's demand starts run `launch_project` too, and a board
    service restart runs none of this: none of them may open a tab. So the callers are named here."""
    callers = {}
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        found = len(re.findall(r"launcher\.present_project_board\(", text))
        if found:
            callers[str(path.relative_to(ROOT))] = found
    check(callers == {"scripts/command_crossing.py": 1, "scripts/launcher_dispatch.py": 1,
                      "scripts/new_project_phases.py": 1, "scripts/resume_provision_command.py": 1,
                      "scripts/switchyard_dispatch.py": 2},
          f"only the explicit launch commands present the board: {callers}")
    for module in ("project_launch", "launch_phases"):
        text = (ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8")
        check("present_project_board" not in text, f"{module}, which every internal launch runs, does not open it")


def test_the_dry_run_plan_names_the_tab() -> None:
    from scripts import launch_phases

    cfg = SimpleNamespace(project="p680", board_url="", desktop_access={"mode": "headless"}, roles=[],
                          run_as_user="p680-agent", repository=None)
    out = io.StringIO()
    with mock.patch.object(board_tab, "board_url", lambda *_a, **_k: (URL, "")), redirect_stdout(out), ExitStack() as s:
        for name, value in {"materialize_layout": lambda *_a, **_k: None,
                            "resolve_layout_mode": lambda *_a, **_k: team_launcher.LAYOUT_MODE_SEPARATE,
                            "viewer_session_for_project": lambda _p: "v",
                            "visible_roles_for_viewer": lambda _c: []}.items():
            s.enter_context(mock.patch.object(team_launcher, name, value))
        planned = plan_of(launch_phases, cfg)
    check(planned.get("board_tab") == {"url": URL, "action": "print the address: the project has no desktop access"},
          f"`team-launcher <p> start --dry-run` says what it would do with the tab: {planned}")


def plan_of(launch_phases, cfg) -> dict:
    """The dry-run plan `_write_layout_and_plan` prints, for a start."""
    import inspect
    import json

    out = io.StringIO()
    kwargs = {name: None for name in inspect.signature(launch_phases._write_layout_and_plan).parameters}
    kwargs.update(config=cfg, config_path=Path("/x/p680.json"), dry_run=True, failed_roles={}, force_reload=False,
                  layout_environ=None, layout_mode="auto", mode="attach-or-start", output_path=Path("/x/layout.json"),
                  pane_script_path=Path("/x/team-launcher"), pane_state_dir=None, runner=lambda *a, **k: None,
                  should_assign_layout_owner=False, window_title="p680")
    with redirect_stdout(out):
        try:
            launch_phases._write_layout_and_plan(**kwargs)
        except Exception as exc:  # noqa: BLE001 - said, with what got printed
            raise AssertionError(f"the dry-run plan did not build: {exc!r} {out.getvalue()[-400:]}") from None
    return json.loads(out.getvalue())


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"board_tab_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
