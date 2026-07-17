#!/usr/bin/env python3
"""Regression test: failed POST writes drain their body before keep-alive reuse."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.server import CALLER_ROLE_HEADER, WRITE_TOKEN_HEADER, TicketBoardServer


class StaticBoardApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()

    def store_signature(self) -> tuple[()]:
        return ()

    def snapshot(self) -> dict[str, object]:
        return {
            "tickets": [],
            "errors": [],
            "states": list(STATES),
            "assignees": list(ASSIGNEES),
            "screenshots": [],
            "store_backend": self.store_backend,
            "store_path": "postgres",
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-keepalive-body.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        server = TicketBoardServer(
            ("127.0.0.1", 0),
            StaticBoardApp(frames, assets),
            write_token="current-token",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            body = json.dumps({"text": "body must be drained before next request", "urgent": False})
            conn.request(
                "POST",
                "/api/tickets/PGU-1/actions/add_comment",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body.encode("utf-8"))),
                    CALLER_ROLE_HEADER: "app",
                    WRITE_TOKEN_HEADER: "stale-token",
                },
            )
            bad_write = conn.getresponse()
            bad_write_body = bad_write.read().decode("utf-8", errors="replace")
            assert bad_write.status == 403, (bad_write.status, bad_write_body)

            conn.request("GET", "/api/board", headers={"Connection": "close"})
            board = conn.getresponse()
            board_body = board.read().decode("utf-8", errors="replace")
            assert board.status == 200, (board.status, board_body)
            parsed = json.loads(board_body)
            assert parsed["tickets"] == []
            assert parsed["store_backend"] == "postgres"
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("ticket_board_keepalive_body_drain_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
