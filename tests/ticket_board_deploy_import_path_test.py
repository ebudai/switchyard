#!/usr/bin/env python3
"""Regression test: deployed ticket-board entrypoint imports with top-level package topology."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"


def main() -> int:
    sys.path = [path for path in sys.path if path not in {"", str(ROOT), str(SCRIPTS_DIR)}]
    sys.path.insert(0, str(SCRIPTS_DIR))

    entrypoint_spec = importlib.util.spec_from_file_location("ticket_board_deployed_entrypoint", SCRIPTS_DIR / "ticket-board.py")
    assert entrypoint_spec is not None
    assert entrypoint_spec.loader is not None
    entrypoint = importlib.util.module_from_spec(entrypoint_spec)
    entrypoint_spec.loader.exec_module(entrypoint)

    cli = importlib.import_module("ticket_board.cli")
    server = importlib.import_module("ticket_board.server")

    assert entrypoint.main is cli.main
    assert cli.main is not None
    assert server.TicketBoardServer is not None
    print("ticket_board_deploy_import_path_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
