#!/usr/bin/env python3
"""Regression test: the board defaults to Postgres and JSON is explicit."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ALLOW_JSON_STORE_ENV, JSON_STORE_RETIRED_ERROR, TicketBoardApp
from scripts.ticket_board.cli import parse_args


def make_paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "store", root / "frames", root / "assets"


def main() -> int:
    with patch.object(sys, "argv", ["ticket-board"]):
        args = parse_args()
    assert args.store_backend == "postgres", args.store_backend
    assert args.allow_json_store is False

    with tempfile.TemporaryDirectory(prefix="ticket-board-backend-default.") as tmpdir:
        root = Path(tmpdir)

        store, frames, assets = make_paths(root / "default")
        app = TicketBoardApp(store, frames, assets, store_backend="")
        assert app.store_backend == "postgres", app.store_backend

        store, frames, assets = make_paths(root / "refuse-json")
        try:
            TicketBoardApp(store, frames, assets, store_backend="json")
        except RuntimeError as exc:
            assert JSON_STORE_RETIRED_ERROR in str(exc)
        else:
            raise AssertionError("json backend should require an explicit opt-in")

        store, frames, assets = make_paths(root / "flag-json")
        app = TicketBoardApp(store, frames, assets, store_backend="json", allow_json_store=True)
        assert app.store_backend == "json", app.store_backend

        store, frames, assets = make_paths(root / "env-json")
        with patch.dict(os.environ, {ALLOW_JSON_STORE_ENV: "1"}):
            app = TicketBoardApp(store, frames, assets, store_backend="json")
        assert app.store_backend == "json", app.store_backend

    print("ticket_board_backend_default_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
