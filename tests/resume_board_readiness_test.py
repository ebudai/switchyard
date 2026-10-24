#!/usr/bin/env python3
"""SYRD-670: a resume that starts the board waits until it answers before anything asks it for authority.

After a clean `switchyard stop`, the next start started the board and went
straight on: `systemctl is-active` says `active` for a `Type=simple` unit as
soon as systemd has exec'd it, and the launch then asked the board's write
socket for process authority before the board had opened it -- `ENOENT`, no
panes, and the same command run again a moment later worked.

Here the board is a real HTTP server on a real Unix socket, which the stand-in
`systemctl start` brings up only after it has already reported the unit
active, as systemd does. The resume runs whole (the staged-tooling check and
the listener stood in), and the launch's own authority check is asked the
moment it returns, as `launch_project` asks it. The before-run is main's code,
from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "7c0b292b22e38cbe06e6a2ab91374b4f82eecb2c"  # main before SYRD-670
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"{detail}\nFAILED CHECK: {detail.split(': ', 1)[0]}")
    CHECKS += 1


class Board:
    """A board on a Unix socket, answering the runtime-assignments probe as `project`, from `delay` after start."""

    def __init__(self, socket_path: Path, *, project: str | None = "p", delay: float = 0.4, never: bool = False,
                 status: int = 200, body: bytes | None = None) -> None:
        self.socket_path, self.project, self.delay, self.never, self.status = socket_path, project, delay, never, status
        self.body = body
        self.started = False
        self.server = None
        self.opened_at = 0.0
        self.requests = 0

    def start(self) -> None:
        if self.never or self.started:
            return
        self.started = True

        def open_later() -> None:
            time.sleep(self.delay)
            project, status, board = self.project, self.status, self
            payload = {"authority_mode": "process", "assignments": {}, **({"project": project} if project else {})}

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802 - the http.server name
                    board.requests += 1
                    body = board.body if board.body is not None else (
                        json.dumps(payload).encode() if status == 200 else b"not found")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args) -> None:
                    pass

            class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                daemon_threads = True

                def get_request(self):  # http.server wants a client address
                    request, _ = super().get_request()
                    return request, ("local", 0)

            self.server = Server(str(self.socket_path), Handler)
            self.opened_at = time.monotonic()
            self.server.serve_forever(poll_interval=0.05)

        threading.Thread(target=open_later, daemon=True).start()

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


def scenario(tree: Path, name: str) -> dict:
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import live_boundary

    live_boundary.isolate()
    from scripts import board_services, tenant_release_deploy
    from scripts import team_launcher as launcher

    out: dict = {"name": name}
    with tempfile.TemporaryDirectory(prefix="s670.", dir="/tmp") as tmp:
        socket_path = Path(tmp) / "b.sock"
        board = Board(socket_path, project={"another": "q", "nameless": None}.get(name, "p"),
                      never=name in ("never", "dies"), status=404 if name == "older" else 200,
                      body=b"<html>not a board</html>" if name == "malformed" else None)
        config = SimpleNamespace(project="p", board_socket=str(socket_path), role_state_isolation=True)
        state = {"active": name == "running", "is_active_calls": 0}
        if name in ("running", "ghost"):
            # Already answering: the running board, or -- for the ghost -- something answering as this project on its
            # socket while the unit just started has already exited.
            board.delay = 0.0
            board.start()
            time.sleep(0.3)
        events: list[tuple[float, str]] = []

        def runner(argv, **kwargs):
            if argv[:2] == ["systemctl", "start"]:
                state["active"] = True
                board.start()  # exec'd, and only later listening: what `Type=simple` reports as active
                events.append((time.monotonic(), "systemctl start returned"))
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:2] == ["systemctl", "is-active"]:
                state["is_active_calls"] += 1
                if name == "dies" and state["is_active_calls"] > 3:
                    state["active"] = False  # the board exits on its own before it ever answers
                if name == "ghost" and state["is_active_calls"] > 3:
                    # Exec'd, read back active by the start and by the resume's own look, then exited at once.
                    state["active"] = False
                return subprocess.CompletedProcess(argv, 0 if state["active"] else 3,
                                                   "active\n" if state["active"] else "inactive\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def listener(*_args, **_kwargs):
            events.append((time.monotonic(), "listener started"))
            return []

        saved = {n: getattr(launcher, n) for n in ("ensure_staged_role_tooling", "capture_listener_state",
                                                   "start_owner_listener")}
        saved_supervised = board_services.board_is_supervised
        saved_bound = (tenant_release_deploy.BOARD_READY_SECONDS, tenant_release_deploy.BOARD_READY_POLL_SECONDS)
        launcher.ensure_staged_role_tooling = lambda config, **kwargs: []
        launcher.capture_listener_state = lambda config, **kwargs: "inactive"
        launcher.start_owner_listener = listener
        board_services.board_is_supervised = lambda project: False
        if name in ("never", "dies", "older", "nameless", "malformed", "ghost"):
            tenant_release_deploy.BOARD_READY_SECONDS, tenant_release_deploy.BOARD_READY_POLL_SECONDS = 1.5, 0.1
        said: list[str] = []
        began = time.monotonic()
        try:
            problems = launcher.resume_tenant(config, config_path=Path(tmp) / "p.json", runner=runner,
                                              print_func=said.append)
            returned = time.monotonic()
            requests_during_resume = board.requests
            # What `launch_project` asks next, the moment the resume returns.
            authority = launcher.process_authority_board_compatibility(config) if not problems else (None, "")
        finally:
            for n, value in saved.items():
                setattr(launcher, n, value)
            board_services.board_is_supervised = saved_supervised
            tenant_release_deploy.BOARD_READY_SECONDS, tenant_release_deploy.BOARD_READY_POLL_SECONDS = saved_bound
            board.close()
        start_at = next((at for at, what in events if what == "systemctl start returned"), None)
        listener_at = next((at for at, what in events if what == "listener started"), None)
        out.update(
            problems=problems, said=said, authority=list(authority), took=round(returned - began, 2),
            socket_after_start=(board.opened_at > start_at) if start_at and board.opened_at else None,
            listener_after_socket=(listener_at > board.opened_at) if listener_at and board.opened_at else None,
            listener_started=listener_at is not None, is_active_calls=state["is_active_calls"],
            requests_during_resume=requests_during_resume,
        )
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, name: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
    try:
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree), name],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=60,
                               stdin=subprocess.DEVNULL, cwd=str(tree))
    except subprocess.TimeoutExpired:
        return {"name": name, "timed_out": True}
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(tree: Path) -> None:
    late = run(tree, "late")
    # The resume is back before the board has opened its socket at all, so there is no opening to time.
    check(late["problems"] == [] and late["took"] < 0.3 and late["authority"][0] is False
          and "No such file or directory" in late["authority"][1],
          f"before: the resume returned with the board started and its socket not yet there, and the launch's "
          f"authority check found nothing: {late}")
    nameless = run(tree, "nameless")
    check(nameless["problems"] == [] and nameless["listener_started"],
          f"before: a responder on the socket that names no project is taken for the board, and the listener started: "
          f"{nameless}")
    never = run(tree, "never")
    check(never["problems"] == [] and never["listener_started"],
          f"before: a board that never answers is reported started, and the listener started after it: {never}")


def candidate() -> None:
    late = run(ROOT, "late")
    check(late["problems"] == [] and late["socket_after_start"] is True and late["listener_after_socket"] is True,
          f"a board whose socket opens after systemctl start returns is waited for: the listener only after it: {late}")
    check(late["authority"] == [True, "process authority ready"],
          f"and the launch's authority check, asked the moment the resume returns, finds it answering: {late['authority']}")
    running = run(ROOT, "running")
    # Two looks, as on main: the one that decides, and the resume's existing "did not come up" check.
    check(running["problems"] == [] and running["is_active_calls"] == 2 and running["took"] < 0.3
          and running["requests_during_resume"] == 0
          and running["said"][0].startswith("already running board"),
          f"an already-running board is not waited on: no look beyond main's two, no probe loop: {running}")
    never = run(ROOT, "never")
    check(not never.get("timed_out"), f"a board that never answers is not waited on past the bound: {never}")
    problem = " ".join(never["problems"])
    check(never["problems"] and not never["listener_started"] and 1.4 <= never["took"] < 4
          and all(part in problem for part in ("p-ticket-board.service", "b.sock", "within 1.5s", "unit is active",
                                               "journalctl -u p-ticket-board.service", "no listener and no pane")),
          f"a board that never answers fails within the bound, naming the unit, the socket, the bound, its state and "
          f"its log, and starts nothing after it: {never}")
    dies = run(ROOT, "dies")
    check(dies["problems"] and "stopped before it answered" in dies["problems"][0] and "not active" in dies["problems"][0]
          and dies["took"] < 1.4 and not dies["listener_started"],
          f"a board whose unit stops before it answers ends the wait early: {dies}")


def not_this_board() -> None:
    """Something answering on the socket that is not this project's board, or not from its running unit, is refused at
    once, before the listener, with what answered (Audit of 4ae2184d)."""
    for name, said in (("older", "HTTP 404"), ("nameless", "names no project"), ("malformed", "not JSON"),
                       ("another", "answers as 'q', not 'p'"),
                       ("ghost", "the unit is no longer active, so the answer is not from it")):
        got = run(ROOT, name)
        check(got["problems"] and said in got["problems"][0] and "journalctl -u p-ticket-board.service" in got["problems"][0]
              and not got["listener_started"] and got["took"] < 1.4,
              f"{name}: refused at once, before the listener, saying what answered: {got}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3]), default=str))
        return 0
    candidate()
    not_this_board()
    if os.environ.get("SYRD670_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd670-before.") as tmp:
            before(tree_at(BEFORE, Path(tmp) / "before"))
    print(f"resume_board_readiness_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
