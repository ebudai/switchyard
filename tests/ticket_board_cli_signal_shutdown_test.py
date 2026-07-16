#!/usr/bin/env python3
"""Regression test: SIGTERM unblocks the ticket-board server loops."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import cli as cli_module


class FakeApp:
    store_backend = "postgres"

    def __init__(self, frame_dir: Path, asset_dir: Path, database_url: str) -> None:
        self.frame_dir = frame_dir
        self.asset_dir = asset_dir
        self.database_url = database_url


class FakeEventHub:
    closed = False

    def __init__(self, _app: object) -> None:
        return

    def close(self) -> None:
        type(self).closed = True


class FakeDirectorNotifier:
    closed = False

    def close(self) -> None:
        type(self).closed = True


class BlockingServer:
    server_address = ("127.0.0.1", 8770)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.shutdown_called = False
        self.closed = False
        self._stopped = threading.Event()

    def serve_forever(self) -> None:
        if not self._stopped.wait(timeout=5):
            raise TimeoutError("serve_forever was not interrupted by shutdown")

    def shutdown(self) -> None:
        self.shutdown_called = True
        self._stopped.set()

    def server_close(self) -> None:
        self.closed = True


class BlockingUnixServer(BlockingServer):
    instances: list["BlockingUnixServer"] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        super().__init__(*_args, **_kwargs)
        type(self).instances.append(self)


class BlockingHttpServer(BlockingServer):
    instances: list["BlockingHttpServer"] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        super().__init__(*_args, **_kwargs)
        type(self).instances.append(self)


def main() -> int:
    args = SimpleNamespace(
        frames="/tmp/pgu-frames",
        assets="/tmp/pgu-assets",
        database="",
        host="127.0.0.1",
        port=8770,
        unix_socket="/tmp/pgu-ticket-board-test.sock",
        open_browser=False,
    )

    def terminate_self() -> None:
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=terminate_self, daemon=True)
    killer.start()

    with patch.object(cli_module, "TicketBoardApp", FakeApp), patch.object(
        cli_module,
        "TicketBoardEventHub",
        FakeEventHub,
    ), patch.object(cli_module, "DirectorNotifier", FakeDirectorNotifier), patch.object(
        cli_module,
        "TicketBoardServer",
        BlockingHttpServer,
    ), patch.object(cli_module, "TicketBoardUnixServer", BlockingUnixServer):
        assert cli_module.run_server(args) == 0

    http_server = BlockingHttpServer.instances[-1]
    unix_server = BlockingUnixServer.instances[-1]
    assert http_server.shutdown_called, "SIGTERM should request HTTP server shutdown"
    assert unix_server.shutdown_called, "SIGTERM should request Unix socket server shutdown"
    assert http_server.closed, "HTTP server socket should close during cleanup"
    assert unix_server.closed, "Unix socket server should close during cleanup"
    assert FakeEventHub.closed, "event hub should close during cleanup"
    assert FakeDirectorNotifier.closed, "director notifier should close during cleanup"

    print("ticket_board_cli_signal_shutdown_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
