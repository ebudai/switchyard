#!/usr/bin/env python3
"""Compatibility entrypoint for the ticket-board browser tool."""

import sys
sys.dont_write_bytecode = True

from ticket_board.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
