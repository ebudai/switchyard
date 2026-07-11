#!/usr/bin/env python3
"""Regression test: board HTML responses disable caching so redeploys refresh normally."""

from __future__ import annotations

import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.server import TicketBoardServer


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
    with tempfile.TemporaryDirectory(prefix="board-cache-headers.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        server = TicketBoardServer(
            ("127.0.0.1", 0),
            StaticBoardApp(frames, assets),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                assert response.status == 200
                assert response.headers.get("Cache-Control") == "no-cache", response.headers
                body = response.read().decode("utf-8")
                assert "<!doctype html>" in body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("board_cache_headers_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
