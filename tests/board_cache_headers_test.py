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

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board.server import TicketBoardServer


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="board-cache-headers.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()

        server = TicketBoardServer(("127.0.0.1", 0), TicketBoardApp(store, frames, assets, store_backend="json", allow_json_store=True))
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
