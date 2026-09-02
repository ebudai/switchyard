#!/usr/bin/env python3
"""Regression test: the ticket board has no selectable JSON backend."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import CALLER_ROLES, TicketBoardApp
from scripts.ticket_board import cli as cli_module
from scripts.ticket_board import server as server_module
from scripts.ticket_board.cli import parse_args


class OneShotServer:
    server_address = ("127.0.0.1", 0)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return

    def serve_forever(self) -> None:
        return

    def server_close(self) -> None:
        return


def failing_unix_server(*_args: object, **_kwargs: object) -> object:
    raise PermissionError("foreign stale socket")


class FakeApp:
    store_backend = "postgres"

    def __init__(self, frame_dir: Path, asset_dir: Path, database_url: str, **_kwargs: object) -> None:
        self.frame_dir = frame_dir
        self.asset_dir = asset_dir
        self.database_url = database_url


class FakeEventHub:
    def __init__(self, _app: object) -> None:
        return

    def close(self) -> None:
        return


def main() -> int:
    assert server_module.DEFAULT_DIRECTORCTL == "/home/agent/bin/directorctl"

    with patch.object(sys, "argv", ["ticket-board"]):
        args = parse_args()
    assert not hasattr(args, "store_backend")
    assert not hasattr(args, "allow_" + "json_store")
    assert not hasattr(args, "store")
    assert args.unix_socket == "/run/pgu-ticket-board/ticket-board.sock"

    with tempfile.TemporaryDirectory(prefix="ticket-board-postgres-only.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        retired_path = root / "store"
        app = TicketBoardApp(frames, assets, database_url="")
        assert getattr(app, "store_backend") == "postgres"
        assert not retired_path.exists(), "postgres-only app should not create a JSON store directory"
        app.list_tickets = lambda: ([], [])  # type: ignore[method-assign]
        app.list_screenshots = lambda: []  # type: ignore[method-assign]
        snapshot = app.snapshot()
        assert snapshot["caller_roles"] == list(CALLER_ROLES)
        assert set(server_module.CALLER_ROLES) == set(CALLER_ROLES)

        args = SimpleNamespace(
            frames=str(frames),
            assets=str(assets),
            database="",
            host="127.0.0.1",
            port=0,
            unix_socket=str(root / "foreign.sock"),
            open_browser=False,
        )
        with patch.object(cli_module, "TicketBoardApp", FakeApp), patch.object(
            cli_module,
            "TicketBoardEventHub",
            FakeEventHub,
        ), patch.object(cli_module, "TicketBoardServer", OneShotServer), patch.object(cli_module, "TicketBoardUnixServer", failing_unix_server):
            assert cli_module.run_server(args) == 0

    print("ticket_board_backend_default_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
