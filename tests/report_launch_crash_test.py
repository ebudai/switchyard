#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "report_launch_crash.py"


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class TicketHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    next_ticket_number = 9000

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        ticket_id = f"PGU-{self.__class__.next_ticket_number}"
        self.__class__.next_ticket_number += 1
        body = json.dumps({"ticket": {"id": ticket_id}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return


def run_reporter(board_url: str, dedupe_dir: str, log_file: str, exit_status: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--launcher",
            "run-stage3b-interactive.sh",
            "--command",
            "/tmp/fake/pgu",
            "--log-file",
            log_file,
            "--exit-status",
            str(exit_status),
            "--cwd",
            "/tmp/fake",
            "--board-url",
            board_url,
            "--dedupe-dir",
            dedupe_dir,
            "--dedupe-seconds",
            "300",
            "--log-lines",
            "3",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), TicketHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    TicketHandler.requests = []
    board_url = f"http://127.0.0.1:{server.server_port}/api/tickets"

    try:
        with tempfile.TemporaryDirectory(prefix="report-launch-crash.") as tmpdir:
            tmp_path = Path(tmpdir)
            log_path = tmp_path / "launch.log"
            log_path.write_text("line one\nline two\nfatal boom\n", encoding="utf-8")

            first = run_reporter(board_url, str(tmp_path / "dedupe"), str(log_path), 1)
            assert_true(first.returncode == 0, f"first reporter run failed: {first.stderr}")
            assert_true(len(TicketHandler.requests) == 1, "first crash should create one ticket")
            first_payload = TicketHandler.requests[0]
            assert_true(first_payload["title"] == "crash on startup: exit 1", "title should encode exit status")
            assert_true(first_payload["assignee"] == "unassigned", "crash tickets should be unassigned")
            assert_true("fatal boom" in first_payload["body"], "ticket body should include the captured log tail")

            second = run_reporter(board_url, str(tmp_path / "dedupe"), str(log_path), 1)
            assert_true(second.returncode == 0, f"second reporter run failed: {second.stderr}")
            assert_true(len(TicketHandler.requests) == 1, "duplicate crash should be suppressed")

            third = run_reporter(board_url, str(tmp_path / "dedupe"), str(log_path), 139)
            assert_true(third.returncode == 0, f"signal reporter run failed: {third.stderr}")
            assert_true(len(TicketHandler.requests) == 2, "different crash reasons should create a new ticket")
            assert_true(
                TicketHandler.requests[1]["title"] == "crash on startup: signal 11",
                "signal exits should be labeled with the signal number",
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    print("report_launch_crash_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
