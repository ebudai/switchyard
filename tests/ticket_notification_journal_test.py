#!/usr/bin/env python3
import importlib.util
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "ticket_notification_journal.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ticket_notification_journal", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def main() -> int:
    journal = load_module()

    assert_equal(
        journal.notification_pairs_for_message("pgu-director:0.0", "New ticket for you: PGU-81 -- Tailscale"),
        ["PGU-81|analysis"],
        "director analysis notification should map to analysis pair",
    )
    assert_equal(
        journal.notification_pairs_for_message("pgu-director:0.0", "PGU-81 -- Tailscale kicked back to you"),
        ["PGU-81|analysis"],
        "director kickback notification should map back to analysis",
    )
    assert_equal(
        journal.notification_pairs_for_message("pgu-app:0.0", "PGU-81 -- Tailscale kicked back to you"),
        ["PGU-81|in_progress"],
        "kickback should map to assignee in_progress pair",
    )
    assert_equal(
        journal.notification_pairs_for_message("pgu-app:0.0", "Ready ticket for you: PGU-81 -- Tailscale"),
        ["PGU-81|in_progress"],
        "legacy ready notification should map to assignee in_progress pair",
    )
    assert_equal(
        journal.notification_pairs_for_message("pgu-audit:0.0", "PGU-81 -- Tailscale ready for audit"),
        ["PGU-81|audit"],
        "audit notification should map to audit pair",
    )
    assert_equal(
        journal.notification_pairs_for_message(
            "pgu-director:0.0",
            "PGU-81 -- Tailscale Eric signed off; ready for director completion",
        ),
        ["PGU-81|eric_review"],
        "signed-off Eric review notification should map to eric_review pair",
    )
    assert_equal(
        journal.notification_pairs_for_message("pgu-director:0.0", "For Eric: 2 tickets ready for your review: PGU-81,82"),
        [],
        "Eric review digest should not claim ticket/state pairs",
    )

    with tempfile.TemporaryDirectory(prefix="ticket-notification-journal.") as tmpdir:
        journal_path = Path(tmpdir) / "journal.jsonl"
        wrote = journal.append_notification_record(
            str(journal_path),
            target="pgu-director:0.0",
            payload="New ticket for you: PGU-81 -- Tailscale",
            sent_at=1234.0,
        )
        assert_equal(wrote, True, "record append should succeed for mapped notification")
        wrote = journal.append_notification_record(
            str(journal_path),
            target="pgu-director:0.0",
            payload="Store error: invalid assignee: research on PGU-57,60,61",
            sent_at=1235.0,
        )
        assert_equal(wrote, False, "store-error message should not be journaled as a ticket/state pair")
        assert_equal(
            journal.load_notification_pairs(str(journal_path)),
            {"PGU-81|analysis"},
            "loader should return only mapped ticket/state pairs",
        )

    print("ticket_notification_journal_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
