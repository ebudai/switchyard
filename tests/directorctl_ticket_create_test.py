#!/usr/bin/env python3
"""Regression test: directorctl ticket-create writes through the board API."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Process, Queue
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKERS = 8


class CreateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        assert self.path == "/api/tickets/actions/create_ticket", self.path
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        ticket_id = self.server.next_ticket_id()  # type: ignore[attr-defined]
        ticket = {
            "id": ticket_id,
            "title": payload["title"],
            "body": payload["body"],
            "assignee": payload["assignee"],
            "state": payload["state"],
            "blocked_by": payload["blocked_by"],
            "blocked_reason": payload["blocked_reason"],
            "implementation": payload["implementation"],
            "comments": [{"who": "director", "text": payload["comment_text"], "ts": "2026-07-11T00:00:00+00:00"}]
            if payload.get("comment_text")
            else [],
            "screenshots": [],
            "screenshot": None,
        }
        self.server.created.append(ticket)  # type: ignore[attr-defined]
        body = json.dumps({"ticket": ticket}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CreateServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), CreateHandler)
        self._next_number = 1
        self._lock = threading.Lock()
        self.created: list[dict[str, object]] = []

    def next_ticket_id(self) -> str:
        with self._lock:
            ticket_id = f"PGU-{self._next_number}"
            self._next_number += 1
            return ticket_id


def run_create(index: int, board_url: str, directorctl: str, queue: Queue[dict[str, object]]) -> None:
    cmd = [
        directorctl,
        "ticket-create",
        "--board-url",
        board_url,
        "--assignee",
        "app",
        "--state",
        "in_progress",
        "--blocked-by",
        "PGU-23, PGU-25",
        "--blocked-reason",
        "Waiting on PGU-23 and PGU-25.",
        "--title",
        f"cli collision test {index}",
        "--body",
        f"worker {index}",
        "--comment-who",
        "director",
        "--comment-text",
        f"created from worker {index}",
        "--implementation",
        f"implementation {index}",
    ]
    completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
    queue.put(json.loads(completed.stdout))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="directorctl-ticket-create.") as tmpdir:
        directorctl = str(Path(tmpdir) / "bin" / "directorctl")
        subprocess.run(
            [str(ROOT / "scripts" / "install-directorctl")],
            check=True,
            env={**os.environ, "DIRECTORCTL_INSTALL_PATH": directorctl},
            stdout=subprocess.DEVNULL,
        )
        server = CreateServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        board_url = f"http://127.0.0.1:{server.server_port}"
        old_socket = os.environ.get("TICKET_BOARD_SOCKET")
        old_legacy_socket = os.environ.get("PGU_TICKET_BOARD_SOCKET")
        os.environ["TICKET_BOARD_SOCKET"] = str(Path(tmpdir) / "ambient-ticket-board.sock")
        os.environ.pop("PGU_TICKET_BOARD_SOCKET", None)

        try:
            single = subprocess.run(
                [
                    directorctl,
                    "ticket-create",
                    "--board-url",
                    board_url,
                    "--assignee",
                    "director",
                    "--state",
                    "in_progress",
                    "--blocked-by",
                    "PGU-23 PGU-25",
                    "--blocked-reason",
                    "Waiting on PGU-23 and PGU-25.",
                    "--title",
                    "single create",
                    "--body",
                    "single body",
                    "--comment-who",
                    "director",
                    "--comment-text",
                    "speced + routed",
                    "--implementation",
                    "ship the helper",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            ticket = json.loads(single.stdout)
            assert ticket["assignee"] == "director"
            assert ticket["state"] == "in_progress"
            assert ticket["screenshots"] == []
            assert ticket["screenshot"] is None
            assert ticket["comments"][0]["who"] == "director"
            assert ticket["comments"][0]["text"] == "speced + routed"
            assert ticket["blocked_by"] == ["PGU-23", "PGU-25"]
            assert ticket["blocked_reason"] == "Waiting on PGU-23 and PGU-25."
            assert ticket["implementation"] == "ship the helper"

            queue: Queue[dict[str, object]] = Queue()
            workers = [Process(target=run_create, args=(index, board_url, directorctl, queue)) for index in range(WORKERS)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                assert worker.exitcode == 0, f"worker exitcode={worker.exitcode}"

            created = [queue.get(timeout=2) for _ in workers]
            ids = [item["id"] for item in created]
            assert len(ids) == WORKERS
            assert len(set(ids)) == WORKERS, ids
        finally:
            if old_socket is None:
                os.environ.pop("TICKET_BOARD_SOCKET", None)
            else:
                os.environ["TICKET_BOARD_SOCKET"] = old_socket
            if old_legacy_socket is None:
                os.environ.pop("PGU_TICKET_BOARD_SOCKET", None)
            else:
                os.environ["PGU_TICKET_BOARD_SOCKET"] = old_legacy_socket
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("directorctl_ticket_create_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
