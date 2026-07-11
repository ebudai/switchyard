#!/usr/bin/env python3
"""Regression test: the ticket board has no selectable JSON backend."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board.cli import parse_args


def main() -> int:
    with patch.object(sys, "argv", ["ticket-board"]):
        args = parse_args()
    assert not hasattr(args, "store_backend")
    assert not hasattr(args, "allow_" + "json_store")
    assert not hasattr(args, "store")

    with tempfile.TemporaryDirectory(prefix="ticket-board-postgres-only.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        retired_path = root / "store"
        app = TicketBoardApp(frames, assets, database_url="")
        assert getattr(app, "store_backend") == "postgres"
        assert not retired_path.exists(), "postgres-only app should not create a JSON store directory"

    print("ticket_board_backend_default_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
