#!/usr/bin/env python3
"""Compatibility entrypoint for the ticket-board browser tool."""

from ticket_board.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
