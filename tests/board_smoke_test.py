#!/usr/bin/env python3
"""SYRD-896: the deploy smoke proves a board is serving this release and reaches its database, at any board size.

On syrd (2026-11-27) two deploys failed and rolled back on "ticket-board smoke check failed for .../api/board: timed
out". The smoke read the whole /api/board -- 29,151,017 bytes, built before its first byte -- with a fixed 1 s socket
timeout per attempt; at load ~41 its first byte took 3.8-4.0 s, so no attempt could pass however long the deadline.

* **The HTTP smoke** (`smoke_check_url` / `smoke_check_http`, as the deploy, canary, post-restart and rollback checks
  run it, from the real `scripts/ticket-board-service-health.sh`):
  - a board whose /api/board is slow and 30 MB still passes, and /api/board is never asked;
  - a board whose /api/build takes 2.5 s to answer -- past the old 1 s cap -- passes inside the deadline;
  - a board whose database read (/api/workflow) fails does not pass, and the failure names the database read;
  - nor does one whose database read answers 200 with an HTML page or a JSON list, not a JSON object (SYRD-898);
  - the defaults are /api/build and /api/workflow (`scripts/ticket-board-service.sh`).
* **The live build-id check** (`verify_live_build_id`) reads /api/build: the right build passes, another fails, and
  /api/board is never asked.
* **The socket smoke** (`scripts/ticket-board-socket-smoke`) proves the socket serves with /api/build, waiting the time
  left rather than a fixed second: an answer after 2.5 s passes.
* **The supervisor migration's probe** (`board_supervisor_migration.default_probe`) asks /api/build and /api/workflow:
  a slow /api/board passes, a failing database read does not.
* **A real board** (the real server and app on a temporary PostgreSQL cluster): passes the smoke; the same server
  whose database cannot be reached answers /api/build and still fails it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

HEALTH = ROOT / "scripts" / "ticket-board-service-health.sh"
SERVICE = ROOT / "scripts" / "ticket-board-service.sh"
SOCKET_SMOKE = ROOT / "scripts" / "ticket-board-socket-smoke"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class FakeBoard(ThreadingHTTPServer):
    """A board whose paths answer as told: {path: (delay_seconds, status, body)}; every path asked is recorded."""

    daemon_threads = True

    def __init__(self, answers: dict[str, tuple[float, int, bytes]]) -> None:
        self.answers, self.asked = answers, []
        super().__init__(("127.0.0.1", 0), FakeHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class FakeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.server.asked.append(self.path)
        delay, status, body = self.server.answers.get(self.path, (0.0, 404, b"{}"))
        time.sleep(delay)
        try:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *_args: object) -> None:
        return


BUILD = (0.0, 200, json.dumps({"build_id": "abc123"}).encode())
WORKFLOW = (0.0, 200, json.dumps({"revision": 7, "document": {"stages": []}}).encode())
SLOW_BIG_BOARD = (6.0, 200, b"[" + b"0," * (15 * 1024 * 1024) + b"0]")


def health(board: FakeBoard, call: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    port = board.server_address[1]
    script = f"""set -euo pipefail
log() {{ echo "LOG $*"; }}
source {HEALTH}
BOARD_HOST=127.0.0.1 BOARD_PORT={port} SMOKE_PATH=/api/build SMOKE_DATABASE_PATH=/api/workflow
SMOKE_TIMEOUT_SECONDS={timeout}
{call}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)


def test_the_http_smoke() -> None:
    board = FakeBoard({"/api/build": BUILD, "/api/workflow": WORKFLOW, "/api/board": SLOW_BIG_BOARD})
    try:
        started = time.monotonic()
        done = health(board, "smoke_check_http")
        took = time.monotonic() - started
        check(done.returncode == 0 and took < 4 and "/api/board" not in board.asked
              and board.asked[:2] == ["/api/build", "/api/workflow"],
              f"a board whose /api/board is slow and 30 MB passes, asking only the build and the database read: "
              f"{done.returncode} {took:.1f}s {board.asked} {done.stderr[-400:]}")
    finally:
        board.shutdown()
    board = FakeBoard({"/api/build": (2.5, *BUILD[1:]), "/api/workflow": WORKFLOW})
    try:
        done = health(board, "smoke_check_http", timeout=8)
        check(done.returncode == 0, f"a first byte after 2.5 s, past the old 1 s cap, passes inside the deadline: "
                                    f"{done.stderr[-400:]}")
    finally:
        board.shutdown()
    board = FakeBoard({"/api/build": BUILD, "/api/workflow": (0.0, 500, b"database unreachable")})
    try:
        done = health(board, "smoke_check_http", timeout=2)
        check(done.returncode != 0 and "smoke check failed" in done.stderr and "/api/workflow" in done.stderr,
              f"a board that cannot read its database fails, naming the database read: {done.stderr[-400:]}")
    finally:
        board.shutdown()
    # SYRD-898: a 200 that is not this release's board reading its database -- another service, or a proxy page
    # answering every path -- is no proof: the database read must answer a JSON object.
    for what, body in (("an HTML page", b"<!doctype html><html><body>Welcome to nginx</body></html>"),
                       ("a JSON list", b"[]")):
        board = FakeBoard({"/api/build": BUILD, "/api/workflow": (0.0, 200, body)})
        try:
            done = health(board, "smoke_check_http", timeout=2)
            check(done.returncode != 0 and "smoke check failed" in done.stderr and "/api/workflow" in done.stderr
                  and "HTTP smoke check passed" not in done.stdout,
                  f"a 200 from the database read whose body is {what}, not a JSON object, fails, naming the database "
                  f"read: {done.returncode} {done.stderr[-300:]}")
        finally:
            board.shutdown()
    defaults = SERVICE.read_text()
    check('SMOKE_PATH="${BOARD_SMOKE_PATH:-/api/build}"' in defaults
          and 'SMOKE_DATABASE_PATH="${BOARD_SMOKE_DATABASE_PATH:-/api/workflow}"' in defaults,
          "the deploy's defaults are the build and the database read")
    check("timeout=1.0" not in HEALTH.read_text(), "no board probe in the health script caps a request at 1 s")
    canary = next((line for line in HEALTH.read_text().splitlines() if "$BOARD_CANARY_TIMEOUT_SECONDS" in line), "")
    check('smoke_check_url "http://$BOARD_HOST:$port$SMOKE_PATH" "$BOARD_CANARY_TIMEOUT_SECONDS" \\' == canary.strip()
          and '"http://$BOARD_HOST:$port$SMOKE_DATABASE_PATH" || status=$?' in HEALTH.read_text(),
          f"the release canary runs the same smoke, the database read included: {canary.strip()}")


def test_the_live_build_id() -> None:
    board = FakeBoard({"/api/build": BUILD, "/api/board": SLOW_BIG_BOARD})
    try:
        good = health(board, "verify_live_build_id abc123")
        bad = health(board, "verify_live_build_id def456", timeout=1.5)
        check(good.returncode == 0 and bad.returncode != 0 and "build_id 'abc123' != expected 'def456'" in bad.stderr
              and "/api/board" not in board.asked,
              f"the live build is read from /api/build alone: {good.returncode} {bad.stderr[-300:]} {board.asked}")
    finally:
        board.shutdown()


def test_the_socket_smoke() -> None:
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("ticket_board_socket_smoke", str(SOCKET_SMOKE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    probe = importlib.util.module_from_spec(spec)
    loader.exec_module(probe)
    check(probe.READ_REQUEST.startswith(b"GET /api/build "), f"it reads the build: {probe.READ_REQUEST[:24]!r}")
    with tempfile.TemporaryDirectory(prefix="s896.", dir="/tmp") as tmp:
        path = os.path.join(tmp, "board.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(4)
        asked: list[bytes] = []

        def answer() -> None:
            conn, _ = server.accept()
            with conn:
                asked.append(conn.recv(4096))
                time.sleep(2.5)  # a busy host: past the old fixed second
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

        threading.Thread(target=answer, daemon=True).start()
        problem = probe.wait_until_serving(path, time.monotonic() + 8)
        server.close()
    check(problem is None and asked and asked[0].startswith(b"GET /api/build "),
          f"an answer after 2.5 s proves the socket serves, waiting the time left: {problem} {asked[:1]}")


def test_the_supervisor_probe() -> None:
    from scripts import board_supervisor_migration as bsm

    board = FakeBoard({"/api/build": BUILD, "/api/workflow": WORKFLOW, "/api/board": SLOW_BIG_BOARD})
    try:
        ok, said = bsm.default_probe(board.server_address[1], 5)
        check(ok and "/api/board" not in board.asked, f"a slow /api/board does not hold it back: {said} {board.asked}")
    finally:
        board.shutdown()
    board = FakeBoard({"/api/build": BUILD, "/api/workflow": (0.0, 500, b"no database")})
    try:
        ok, said = bsm.default_probe(board.server_address[1], 1.5)
        check(not ok and "/api/workflow" in said, f"a failing database read does: {said}")
    finally:
        board.shutdown()


def test_a_real_board() -> None:
    from router_role_board_test import build_board, fixture, temporary_cluster

    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_"))]:
        os.environ.pop(key)
    with temporary_cluster(prefix="syrd896-", shutdown="immediate") as cluster:
        build_board(cluster, "smoke")
        for name, url in (("reachable", fixture.conninfo(cluster.socket_dir, cluster.port, "smoke", fixture.SERVICE_ROLE)),
                          ("unreachable", f"host={cluster.root / 'no-such-socket-dir'} port=1 dbname=smoke user=nobody "
                                          "connect_timeout=1")):
            app = fixture.TicketBoardApp(cluster.root / f"{name}-frames", cluster.root / f"{name}-assets",
                                         project="pgu", ticket_prefix="PGU", database_url=url)
            server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_port
                script = f"""set -euo pipefail
log() {{ echo "LOG $*"; }}
source {HEALTH}
BOARD_HOST=127.0.0.1 BOARD_PORT={port} SMOKE_PATH=/api/build SMOKE_DATABASE_PATH=/api/workflow SMOKE_TIMEOUT_SECONDS=4
smoke_check_http
"""
                done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/build", timeout=10) as response:
                    build_ok = response.status == 200
                if name == "reachable":
                    check(done.returncode == 0 and build_ok, f"the real board passes: {done.stderr[-400:]}")
                else:
                    check(done.returncode != 0 and build_ok,
                          f"the same board whose database cannot be reached answers /api/build and still fails: "
                          f"{done.returncode} {done.stderr[-300:]}")
            finally:
                server.shutdown()
                server.server_close()


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"board_smoke_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
