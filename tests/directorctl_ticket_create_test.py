#!/usr/bin/env python3
"""Regression test: directorctl ticket-create writes through the board API."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from multiprocessing import Process, Queue
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board.server import TicketBoardServer

DIRECTORCTL = ROOT / "scripts" / "directorctl"
WORKERS = 8


class QuietNotifier:
    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        return

    def close(self) -> None:
        return


def run_create(index: int, board_url: str, queue: Queue[dict[str, str]]) -> None:
    cmd = [
        str(DIRECTORCTL),
        "ticket-create",
        "--board-url",
        board_url,
        "--assignee",
        "app",
        "--state",
        "ready",
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
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()
        server = TicketBoardServer(("127.0.0.1", 0), TicketBoardApp(store, frames, assets, store_backend="json", allow_json_store=True), director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        board_url = f"http://127.0.0.1:{server.server_port}"

        try:
            single = subprocess.run(
                [
                    str(DIRECTORCTL),
                    "ticket-create",
                    "--board-url",
                    board_url,
                    "--assignee",
                    "director",
                    "--state",
                    "ready",
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
            assert ticket["state"] == "ready"
            assert ticket["screenshots"] == []
            assert ticket["screenshot"] is None
            assert ticket["comments"][0]["who"] == "director"
            assert ticket["comments"][0]["text"] == "speced + routed"
            assert ticket["comments"][0]["ts"] == ticket["created"]
            assert ticket["blocked_by"] == ["PGU-23", "PGU-25"]
            assert ticket["blocked_reason"] == "Waiting on PGU-23 and PGU-25."
            assert ticket["implementation"] == "ship the helper"

            loaded = json.loads((store / f"{ticket['id']}.json").read_text(encoding="utf-8"))
            assert loaded["state"] == "ready"
            assert loaded["comments"][0]["ts"] == loaded["created"]

            queue: Queue[dict[str, str]] = Queue()
            workers = [Process(target=run_create, args=(index, board_url, queue)) for index in range(WORKERS)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                assert worker.exitcode == 0, f"worker exitcode={worker.exitcode}"

            created = [queue.get(timeout=2) for _ in workers]
            ids = [item["id"] for item in created]
            assert len(ids) == WORKERS
            assert len(set(ids)) == WORKERS, ids

            files = sorted(store.glob("PGU-*.json"))
            assert len(files) == WORKERS + 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("directorctl_ticket_create_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
