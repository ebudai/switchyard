#!/usr/bin/env python3
"""Regression test: attachment grids use thumbnails while the lightbox keeps full-res images."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function thumbnailUrlFor(path)" in HTML
    assert "return `/api/thumb/${encodeURIComponent(path)}?w=512`;" in HTML
    assert "image.src = thumbnailUrlFor(entry.path);" in HTML
    assert "image.loading = 'lazy';" in HTML
    assert "image.decoding = 'async';" in HTML
    assert "imageLightboxImageEl.src = previewUrlFor(entry.path);" in HTML
    assert "image.src = previewUrlFor(entry.path);" not in HTML
    print("attachment_thumbnail_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
