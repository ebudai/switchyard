#!/usr/bin/env python3
"""Regression test: attachment galleries use cached thumbnails, not full-res images."""

from __future__ import annotations

import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.server import DirectorNotifier, TicketBoardServer


class QuietNotifier(DirectorNotifier):
    def __init__(self) -> None:
        super().__init__(sender=lambda payload: None, batch_window_seconds=0.01)


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

    def resolve_image(self, raw_path: str) -> Path:
        path = Path(raw_path).resolve()
        if self.asset_dir not in path.parents:
            raise FileNotFoundError(f"invalid image path: {raw_path}")
        return path


def fetch(url: str, headers: dict[str, str] | None = None) -> tuple[int, object, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-thumbs.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        source = assets / "target__large-reference.png"
        Image.new("RGBA", (1800, 1200), color=(42, 96, 180, 255)).save(source)

        server = TicketBoardServer(("127.0.0.1", 0), StaticBoardApp(frames, assets), director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            encoded = urllib.parse.quote(str(source), safe="")
            image_url = f"http://127.0.0.1:{server.server_port}/api/image/{encoded}"
            thumb_url = f"http://127.0.0.1:{server.server_port}/api/thumb/{encoded}?w=512"

            status, headers, full_body = fetch(image_url)
            assert status == 200
            assert headers.get("Cache-Control") == "public, max-age=31536000, immutable"
            assert headers.get("ETag")
            assert headers.get("Last-Modified")
            assert headers.get("Content-Type") == "image/png"
            assert len(full_body) == source.stat().st_size

            status, _, body = fetch(image_url, {"If-None-Match": headers["ETag"]})
            assert status == 304
            assert body == b""

            status, thumb_headers, thumb_body = fetch(thumb_url)
            assert status == 200
            assert thumb_headers.get("Content-Type") == "image/jpeg"
            assert thumb_headers.get("Cache-Control") == "public, max-age=31536000, immutable"
            assert thumb_headers.get("ETag")
            assert thumb_headers.get("Last-Modified")
            assert len(thumb_body) < len(full_body)
            with Image.open(BytesIO(thumb_body)) as thumb:
                assert thumb.format == "JPEG"
                assert max(thumb.size) == 512

            cache_dir = assets / ".thumb-cache"
            cached = list(cache_dir.glob("*.jpg"))
            assert len(cached) == 1
            assert cached[0].read_bytes() == thumb_body

            status, _, body = fetch(thumb_url, {"If-None-Match": thumb_headers["ETag"]})
            assert status == 304
            assert body == b""
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("ticket_board_thumbnail_cache_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
