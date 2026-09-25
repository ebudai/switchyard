#!/usr/bin/env python3
"""The notify listener asks its own board which pane a role is in.

Live MEFP, 2026-09-24: MEFP-1's in_progress/ops notice was claimed from MEFP's
database and then failed on every attempt with

    error: cannot resolve runtime assignment for ops: HTTP Error 404: Not Found

because directorctl resolves a role through TICKET_BOARD_URL, defaulting to
8770, and neither generated listener unit set it. MEFP's board is on 26623;
8770 is another tenant's, or nobody's. SYRD-264 made the listener requeue that
failure instead of dead-lettering it, but it could never succeed (SYRD-265).

These cases render the units the product generates -- fresh, after an upgrade,
and from the legacy service script -- and hand exactly the environment each
declares to the real directorctl, via the real DirectorctlSender, which copies
the listener's own environment. Delivery goes into a tmux server this test
owns (TMUX stripped, socket asserted under a temp dir), never a live one.
"""

from __future__ import annotations

import getpass
import http.server
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

# Before the listener is imported: its default role targets are derived from the
# project at import time. This reproduces MEFP.
os.environ["TICKET_BOARD_PROJECT"] = "mefp"
os.environ.pop("PGU_TICKET_BOARD_PROJECT", None)

import ticket_board_notify_listener_test as lt  # noqa: E402
from scripts.team_launcher import _project_board_provision_from_json, render_privileged_artifacts  # noqa: E402
from scripts.ticket_board import notify_listener as nl  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    allocated_port,
    build_plan,
    render_board_unit,
    render_listener_unit,
    write_artifacts,
)

DIRECTORCTL = ROOT / "scripts" / "directorctl"
LISTENER_SERVICE = ROOT / "scripts" / "ticket-board-notify-listener-service.sh"
PROJECT = "mefp"
TARGET = f"{PROJECT}-ops:0.0"
MESSAGE = "MEFP-1 is yours"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def plan_for(tmp: Path, *, port: int | None = None):
    return build_plan(
        project=PROJECT, owner_user=getpass.getuser(), owner_home=tmp / "home",
        port=port, board_root=tmp / "board", commit_git_dir=str(tmp / "cache.git"),
        asset_dir=tmp / "assets", frame_dir=tmp / "frames",
    )


def environment(unit: str) -> dict[str, str]:
    """Every `Environment=` a unit declares, as systemd would set them."""
    env: dict[str, str] = {}
    for line in unit.splitlines():
        if line.startswith("Environment="):
            for assignment in shlex.split(line[len("Environment="):]):
                key, _, value = assignment.partition("=")
                env[key] = value
    return env


def urls(unit: str) -> list[str]:
    return [line.split("=", 2)[2] for line in unit.splitlines() if line.startswith("Environment=TICKET_BOARD_URL=")]


def board_port(unit: str) -> str:
    (exec_start,) = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    words = shlex.split(exec_start[len("ExecStart="):])
    return words[words.index("--port") + 1]


def test_the_generated_unit_names_the_board_its_tenant_runs() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd265-render.") as tmp:
        for port in (None, 31111):
            plan = plan_for(Path(tmp), port=port)
            unit = render_listener_unit(plan)
            expected = allocated_port(PROJECT) if port is None else port
            check(urls(unit) == [f"http://127.0.0.1:{expected}"], (port, urls(unit)))
            # The same address the board unit binds, not a second guess at it.
            check(board_port(render_board_unit(plan)) == str(expected), (port, render_board_unit(plan)))
        check(allocated_port(PROJECT) == 26623, "MEFP's live board port is its allocated port")


def test_an_upgraded_tenant_gets_the_url_from_its_recorded_plan() -> None:
    """The upgrade regenerates root's copies from the tenant's own plan.json."""
    with tempfile.TemporaryDirectory(prefix="syrd265-upgrade.") as tmp:
        tmp_path = Path(tmp)
        plan = plan_for(tmp_path, port=32323)
        write_artifacts(plan, tmp_path / "provision")
        reloaded = _project_board_provision_from_json(tmp_path / "provision" / "plan.json")
        rendered = render_privileged_artifacts(reloaded)
        unit = rendered[reloaded.listener_unit].decode("utf-8")
        check(urls(unit) == ["http://127.0.0.1:32323"], urls(unit))


def _script_unit(**env: str) -> str:
    base = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "TICKET_BOARD_OWNER_HOME": "/home/x"}
    proc = subprocess.run(
        ["bash", str(LISTENER_SERVICE), "render-unit"], capture_output=True, text=True,
        env={**base, **env}, timeout=60,
    )
    check(proc.returncode == 0, proc.stderr)
    return proc.stdout


def test_the_legacy_service_script_names_the_board_too() -> None:
    check(urls(_script_unit(TICKET_BOARD_PROJECT="pgu")) == ["http://127.0.0.1:8770"], "pgu is unchanged")
    for project in ("mefp", "syrd", "test"):
        check(urls(_script_unit(TICKET_BOARD_PROJECT=project)) == [f"http://127.0.0.1:{allocated_port(project)}"],
              project)
    check(urls(_script_unit(TICKET_BOARD_PROJECT="mefp", BOARD_PORT="31111")) == ["http://127.0.0.1:31111"], "port")
    check(urls(_script_unit(TICKET_BOARD_PROJECT="mefp", TICKET_BOARD_URL="http://127.0.0.1:1234"))
          == ["http://127.0.0.1:1234"], "an explicit URL wins")


class Board(http.server.BaseHTTPRequestHandler):
    """The tenant's board: it knows where ops is running."""

    requests: list[str] = []

    def do_GET(self):  # noqa: N802
        Board.requests.append(self.path)
        if self.path == "/api/runtime-assignments/ops":
            body = json.dumps({"project": PROJECT, "authority_mode": "process",
                               "assignment": {"actual_target": TARGET}}).encode()
            self.send_response(200)
        else:
            body = b"not found"
            self.send_response(404)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _listen_once(unit_env: dict[str, str], row) -> "lt.FakeConnection":
    """One listener pass, with exactly the unit's environment as its own."""
    conn = lt.FakeConnection([row])
    listener = nl.TicketBoardNotifyListener(
        conninfo="dbname=test", project=PROJECT, sender=nl.DirectorctlSender(str(DIRECTORCTL)),
        activity_gate=lambda _target: False, connector=lambda *a, **k: conn,
        poll_seconds=0, target_exists=lambda _target: True,
    )
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(unit_env)
    try:
        listener.listen_once(max_notifications=1)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return conn


def test_a_restart_onto_the_fixed_unit_delivers_the_queued_notice_exactly_once() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="syrd265-deliver."))
    received = tmp / "received"
    host = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp), "TERM": "xterm-256color",
            "TMUX_TMPDIR": str(tmp)}
    try:
        # Like an agent CLI: a submitted line visibly lands on screen, which is
        # what directorctl waits to see, and is recorded once per submission.
        pane = (f"stty -echo; while IFS= read -r line; do printf 'got: %s\\n' \"$line\"; "
                f"printf '%s\\n' \"$line\" >> {received}; done")
        subprocess.run(["tmux", "new-session", "-d", "-s", f"{PROJECT}-ops", "-x", "200", "-y", "20",
                        "bash", "-c", pane], env=host, check=True)
        sock = subprocess.run(["tmux", "display-message", "-p", "#{socket_path}"], env=host,
                              capture_output=True, text=True).stdout.strip()
        check(sock.startswith(str(tmp)), f"a live tmux server was reachable: {sock}")

        plan = plan_for(tmp, port=server.server_port)
        fixed = render_listener_unit(plan)
        before = "\n".join(line for line in fixed.splitlines() if not line.startswith("Environment=TICKET_BOARD_URL="))
        check(urls(before) == [] and urls(fixed) == [f"http://127.0.0.1:{server.server_port}"], "fixture")

        # The unit as it was: directorctl asks 8770, and the notice is requeued.
        first = _listen_once({**host, **environment(before)}, lt.queue_row(41, "MEFP-1", assignee="ops",
                                                                          message=MESSAGE))
        check(first.acked == [] and len(first.requeued) == 1, (first.acked, first.requeued))
        check(first.requeued[0][1][2] == nl.RUNTIME_ASSIGNMENT_UNRESOLVED, first.requeued)
        check(Board.requests == [], f"the tenant's own board was never asked: {Board.requests}")

        # Restarted on the regenerated unit: the same notice, retried.
        second = _listen_once({**host, **environment(fixed)}, lt.queue_row(41, "MEFP-1", assignee="ops",
                                                                           message=MESSAGE, attempts=2))
        check(second.acked == [41], (second.acked, second.requeued, second.dead_lettered))
        check(second.requeued == [] and second.dead_lettered == [], (second.requeued, second.dead_lettered))
        check("/api/runtime-assignments/ops" in Board.requests, Board.requests)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (received.exists() and received.read_text().strip()):
            time.sleep(0.1)
        time.sleep(0.5)
        lines = [line for line in received.read_text().splitlines() if line.strip()] if received.exists() else []
        check(lines.count(MESSAGE) == 1 and len(lines) == 1, f"delivered exactly once: {lines}")
    finally:
        subprocess.run(["tmux", "kill-server"], env=host, capture_output=True)
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"listener_board_url_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
