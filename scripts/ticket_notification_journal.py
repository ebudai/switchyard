#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time


DEFAULT_JOURNAL_PATH = "/tmp/pgu-ticket-notification-journal.jsonl"
DIRECTOR_TARGET = "pgu-director:0.0"
AUDIT_TARGET = "pgu-audit:0.0"
TICKET_ID_RE = re.compile(r"(PGU-\d+)")
LEGACY_STATE_ALIASES = {"open": "analysis"}


def ticket_pair_key(ticket_id: str, state: str) -> str:
    state = LEGACY_STATE_ALIASES.get(state, state)
    return f"{ticket_id}|{state}"


def extract_ticket_ids(payload: str) -> list[str]:
    return TICKET_ID_RE.findall(payload)


def notification_pairs_for_message(target: str, payload: str) -> list[str]:
    normalized = " ".join(payload.split()).strip()
    if not normalized:
        return []

    ticket_ids = extract_ticket_ids(normalized)
    if not ticket_ids:
        return []

    if target == DIRECTOR_TARGET:
        if normalized.startswith("New ticket for you: ") or normalized.startswith("New tickets for you: "):
            return [ticket_pair_key(ticket_id, "analysis") for ticket_id in ticket_ids]
        if normalized.endswith("kicked back to you"):
            return [ticket_pair_key(ticket_id, "analysis") for ticket_id in ticket_ids]
        if normalized.endswith("ready for your review"):
            return [ticket_pair_key(ticket_id, "director_review") for ticket_id in ticket_ids]
        return []

    if target == AUDIT_TARGET and normalized.endswith("ready for audit"):
        return [ticket_pair_key(ticket_id, "audit") for ticket_id in ticket_ids]

    if normalized.startswith("Ready ticket for you: "):
        return [ticket_pair_key(ticket_id, "ready") for ticket_id in ticket_ids]

    if normalized.startswith("New ticket for you: ") or normalized.endswith("kicked back to you"):
        return [ticket_pair_key(ticket_id, "in_progress") for ticket_id in ticket_ids]

    return []


def append_notification_record(journal_path: str, *, target: str, payload: str, sent_at: float | None = None) -> bool:
    pairs = notification_pairs_for_message(target, payload)
    if not pairs:
        return False

    os.makedirs(os.path.dirname(journal_path) or ".", exist_ok=True)
    record = {
        "ts": float(time.time() if sent_at is None else sent_at),
        "target": target,
        "payload": payload,
        "pairs": pairs,
    }
    with open(journal_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")
    return True


def load_notification_pairs(journal_path: str) -> set[str]:
    pairs: set[str] = set()
    try:
        with open(journal_path, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                raw_pairs = record.get("pairs")
                if not isinstance(raw_pairs, list):
                    continue
                for pair in raw_pairs:
                    if isinstance(pair, str):
                        pairs.add(pair)
    except FileNotFoundError:
        return set()
    return pairs


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record or inspect ticket notification journal entries.")
    parser.add_argument("--journal", default=DEFAULT_JOURNAL_PATH, help=f"Journal path (default: {DEFAULT_JOURNAL_PATH}).")

    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record", help="Append a notification record when a message maps to ticket state notifications.")
    record_parser.add_argument("--target", required=True, help="tmux target that received the message")
    record_parser.add_argument("--payload", required=True, help="message payload")

    pairs_parser = subparsers.add_parser("pairs", help="Print known ticket-state pairs from the journal as JSON.")
    pairs_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "record":
        append_notification_record(args.journal, target=args.target, payload=args.payload)
        return 0
    if args.command == "pairs":
        pairs = sorted(load_notification_pairs(args.journal))
        json.dump(pairs, sys.stdout, indent=2 if args.pretty else None)
        sys.stdout.write("\n")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
