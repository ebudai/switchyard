#!/usr/bin/env python3
"""Regression tests for the guarded collation maintenance helper."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ticket-board-refresh-collation-version"


def test_plan_only_prints_locking_repair_sql_without_connecting() -> None:
    result = subprocess.run(
        [
            str(SCRIPT),
            "--plan-only",
            "--database",
            "postgres",
            "--database",
            "pgu",
            "--expected-ticket-count",
            "574",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert 'REINDEX DATABASE "postgres";' in result.stdout
    assert 'ALTER DATABASE "postgres" REFRESH COLLATION VERSION;' in result.stdout
    assert 'REINDEX DATABASE "pgu";' in result.stdout
    assert 'ALTER DATABASE "pgu" REFRESH COLLATION VERSION;' in result.stdout
    assert result.stderr == ""


def test_default_mode_is_dry_run() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "EXECUTE=0" in source
    assert "dry run only; pass --execute during a maintenance window" in source
    assert "REINDEX DATABASE" in source
    assert "ticket count changed during repair" in source


def main() -> int:
    test_plan_only_prints_locking_repair_sql_without_connecting()
    test_default_mode_is_dry_run()
    print("ticket_board_collation_maintenance_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
