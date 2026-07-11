"""CLI entrypoint for the ticket-board browser tool."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from .app import (
    ASSET_DIR_DEFAULT,
    FRAME_DIR_DEFAULT,
    POSTGRES_DSN_DEFAULT,
    TicketBoardApp,
)
from .server import CallerRegistry, DirectorNotifier, TicketBoardEventHub, TicketBoardServer, TicketBoardUnixServer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Serve a lightweight local "Jira but easier" ticket board for PGU.')
    parser.add_argument(
        "--database",
        default=POSTGRES_DSN_DEFAULT,
        help="Postgres connection string. Default: TICKET_BOARD_DATABASE_URL or DATABASE_URL.",
    )
    parser.add_argument("--frames", default=str(FRAME_DIR_DEFAULT), help=f"Screenshot directory. Default: {FRAME_DIR_DEFAULT}")
    parser.add_argument("--assets", default=str(ASSET_DIR_DEFAULT), help=f"Uploaded attachment directory. Default: {ASSET_DIR_DEFAULT}")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8770, help="Bind port. Default: 8770")
    parser.add_argument(
        "--unix-socket",
        default="",
        help="Optional Unix-domain socket path for local pane writes with SO_PEERCRED caller identity.",
    )
    parser.add_argument("--open-browser", action="store_true", help="Open the board URL automatically.")
    return parser.parse_args()


def run_server(args: argparse.Namespace) -> int:
    app = TicketBoardApp(
        frame_dir=Path(args.frames),
        asset_dir=Path(args.assets),
        database_url=args.database,
    )
    event_hub = TicketBoardEventHub(app)
    director_notifier = DirectorNotifier()
    caller_registry = CallerRegistry()
    server = TicketBoardServer(
        (args.host, args.port),
        app,
        events=event_hub,
        director_notifier=director_notifier,
    )
    unix_server = None
    unix_thread = None
    if args.unix_socket:
        unix_server = TicketBoardUnixServer(
            Path(args.unix_socket),
            app,
            events=event_hub,
            director_notifier=director_notifier,
            caller_registry=caller_registry,
        )
        unix_thread = threading.Thread(target=unix_server.serve_forever, name="ticket-board-unix-socket", daemon=True)
        unix_thread.start()
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"[ticket-board] backend: {app.store_backend}")
    print(f"[ticket-board] frames: {app.frame_dir}")
    print(f"[ticket-board] assets: {app.asset_dir}")
    print(f"[ticket-board] listening on {url}")
    if unix_server is not None:
        print(f"[ticket-board] local write socket: {args.unix_socket}")
    print("[ticket-board] press Ctrl+C to stop")
    if args.open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ticket-board] stopping")
    finally:
        if unix_server is not None:
            unix_server.shutdown()
            unix_server.server_close()
        if unix_thread is not None:
            unix_thread.join(timeout=2)
        server.server_close()
        event_hub.close()
        director_notifier.close()
    return 0


def main() -> int:
    return run_server(parse_args())
