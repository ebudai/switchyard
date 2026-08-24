#!/usr/bin/env python3
"""Regression test: the otto milestone runbook matches provisioner output."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.project_provision import build_plan, render_workflow_sql


RUNBOOK = ROOT / "docs" / "otto-milestone-1-hand-standup.md"


def main() -> int:
    text = RUNBOOK.read_text(encoding="utf-8")
    plan = build_plan(project="otto", owner_user="otto-agent")
    workflow = render_workflow_sql(plan)

    assert "Otto Milestone 2 Standup Runbook" in text
    assert str(plan.port) in text
    assert plan.database in text
    assert plan.socket_path in text
    assert plan.board_unit in text
    assert plan.listener_unit in text
    assert "TICKET_BOARD_IMPLEMENTER_ROLES" in text
    assert "draft_roles` is `[\"designer\"]" in text
    assert "implementer_roles` is `[\"ops\"]" in text
    assert "('draft', 'Draft', 0, ARRAY['designer']::text[]" in workflow
    assert "('in_progress', 'Implementation', 2, ARRAY['ops']::text[]" in workflow
    assert "mktemp -d /tmp/otto-board-provision" in text
    assert "rm -rf" not in text
    assert "scripts/ticket-board-write submit-to-audit OTTO-N" in text
    print("otto_milestone2_runbook_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
