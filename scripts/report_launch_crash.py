#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

try:
    from ticket_board.write_client import TicketBoardWriteClient
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.ticket_board.write_client import TicketBoardWriteClient

DEFAULT_BOARD_URL = "http://127.0.0.1:8770"
DEFAULT_DEDUPE_DIR = "/tmp/pgu-launch-crash-dedupe"
DEFAULT_DEDUPE_SECONDS = 300
DEFAULT_LOG_LINE_COUNT = 50


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a PGU ticket for an abnormal launcher exit.")
    parser.add_argument("--launcher", required=True, help="Launcher name, e.g. run-stage3b-interactive.sh")
    parser.add_argument("--command", required=True, help="Command or binary that exited abnormally.")
    parser.add_argument("--log-file", required=True, help="Path to the captured launcher log.")
    parser.add_argument("--exit-status", type=int, required=True, help="Observed process exit status.")
    parser.add_argument("--cwd", required=True, help="Working directory used for the launch.")
    parser.add_argument("--board-url", default=DEFAULT_BOARD_URL, help=f"Ticket board POST endpoint (default: {DEFAULT_BOARD_URL}).")
    parser.add_argument("--dedupe-dir", default=DEFAULT_DEDUPE_DIR, help=f"Directory for recent crash fingerprints (default: {DEFAULT_DEDUPE_DIR}).")
    parser.add_argument("--dedupe-seconds", type=int, default=DEFAULT_DEDUPE_SECONDS, help=f"Suppress duplicate crash tickets for this many seconds (default: {DEFAULT_DEDUPE_SECONDS}).")
    parser.add_argument("--log-lines", type=int, default=DEFAULT_LOG_LINE_COUNT, help=f"Number of log lines to include (default: {DEFAULT_LOG_LINE_COUNT}).")
    return parser.parse_args(argv)


def reason_for_exit_status(exit_status: int) -> str:
    if exit_status >= 128:
        return f"signal {exit_status - 128}"
    return f"exit {exit_status}"


def read_log_tail(path: Path, line_count: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return "<log file missing>"
    if line_count <= 0:
        return ""
    tail = lines[-line_count:]
    return "\n".join(tail).strip()


def fingerprint_payload(launcher: str, command: str, reason: str, log_tail: str) -> str:
    digest = hashlib.sha256()
    for value in (launcher, command, reason, log_tail):
        digest.update(value.encode("utf-8", errors="replace"))
        digest.update(b"\n")
    return digest.hexdigest()


def should_suppress_duplicate(dedupe_dir: Path, fingerprint: str, dedupe_seconds: int) -> bool:
    dedupe_dir.mkdir(parents=True, exist_ok=True)
    stamp_path = dedupe_dir / fingerprint
    now = int(time.time())
    try:
        previous = int(stamp_path.read_text(encoding="utf-8").strip())
    except Exception:
        previous = 0
    if previous and now - previous < dedupe_seconds:
        return True
    stamp_path.write_text(f"{now}\n", encoding="utf-8")
    return False


def build_ticket_payload(args: argparse.Namespace, reason: str, log_tail: str) -> dict[str, object]:
    title = f"crash on startup: {reason}"
    sections = [
        f"Launcher: {args.launcher}",
        f"Command: {args.command}",
        f"Working directory: {args.cwd}",
        f"Exit status: {args.exit_status} ({reason})",
        "",
        f"Last {args.log_lines} log lines:",
        log_tail or "<no log output captured>",
    ]
    return {
        "title": title,
        "body": "\n".join(sections).strip(),
        "assignee": "unassigned",
        "needs_user_signoff": False,
        "screenshot": None,
    }


def create_ticket(board_url: str, payload: dict[str, object]) -> str:
    parsed = TicketBoardWriteClient(board_url, "director").create_ticket(
        title=str(payload["title"]),
        body=str(payload["body"]),
        assignee=str(payload.get("assignee", "unassigned")),
        needs_user_signoff=bool(payload.get("needs_user_signoff", False)),
        screenshot=payload.get("screenshot"),  # type: ignore[arg-type]
    )
    ticket = parsed.get("ticket")
    if not isinstance(ticket, dict) or not isinstance(ticket.get("id"), str):
        raise RuntimeError("ticket board response did not include ticket.id")
    return ticket["id"]


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.exit_status == 0:
        return 0

    log_tail = read_log_tail(Path(args.log_file), args.log_lines)
    reason = reason_for_exit_status(args.exit_status)
    fingerprint = fingerprint_payload(args.launcher, args.command, reason, log_tail)
    if should_suppress_duplicate(Path(args.dedupe_dir), fingerprint, args.dedupe_seconds):
        print(f"[report-launch-crash] duplicate suppressed for {args.launcher}: {reason}", file=sys.stderr)
        return 0

    payload = build_ticket_payload(args, reason, log_tail)
    try:
        ticket_id = create_ticket(args.board_url, payload)
    except Exception as exc:
        print(f"[report-launch-crash] failed to create ticket: {exc}", file=sys.stderr)
        return 1

    print(f"[report-launch-crash] created {ticket_id} for {args.launcher}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
