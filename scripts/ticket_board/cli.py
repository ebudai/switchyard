"""CLI entrypoint for the ticket-board browser tool."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from .app import FRAME_DIR_DEFAULT, POSTGRES_DSN_DEFAULT, STORE_BACKEND_DEFAULT, STORE_DIR_DEFAULT, TicketBoardApp
from .server import TicketBoardServer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Serve a lightweight local "Jira but easier" ticket board for PGU.')
    parser.add_argument("--store", default=str(STORE_DIR_DEFAULT), help=f"Ticket JSON directory. Default: {STORE_DIR_DEFAULT}")
    parser.add_argument(
        "--store-backend",
        choices=("json", "postgres"),
        default=STORE_BACKEND_DEFAULT,
        help=f"Ticket storage backend. Default: {STORE_BACKEND_DEFAULT}",
    )
    parser.add_argument(
        "--database",
        default=POSTGRES_DSN_DEFAULT,
        help="Postgres connection string for --store-backend postgres. Default: TICKET_BOARD_DATABASE_URL or DATABASE_URL.",
    )
    parser.add_argument("--frames", default=str(FRAME_DIR_DEFAULT), help=f"Screenshot directory. Default: {FRAME_DIR_DEFAULT}")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8770, help="Bind port. Default: 8770")
    parser.add_argument("--open-browser", action="store_true", help="Open the board URL automatically.")
    return parser.parse_args()


def run_server(args: argparse.Namespace) -> int:
    app = TicketBoardApp(
        Path(args.store),
        Path(args.frames),
        store_backend=args.store_backend,
        database_url=args.database,
    )
    server = TicketBoardServer((args.host, args.port), app)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"[ticket-board] backend: {app.store_backend}")
    if app.store_backend == "json":
        print(f"[ticket-board] store: {app.store_dir}")
    print(f"[ticket-board] frames: {app.frame_dir}")
    print(f"[ticket-board] listening on {url}")
    print("[ticket-board] press Ctrl+C to stop")
    if args.open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ticket-board] stopping")
    finally:
        server.server_close()
    return 0


def main() -> int:
    return run_server(parse_args())
