#!/usr/bin/env python3
"""A refused project-account migration must fail the upgrade that ran it.

The SYRD-69 contract stops before mutation while a legacy dedicated-account
pane is live. These cases hold the exit status and output together: a nonzero
refusal must not advertise later release or director work as though migration
had succeeded, while a checkpointed or already-repatriated tenant proceeds.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from team_launcher_test_helpers import *
from team_launcher_upgrade_cutover_test import (
    _RunningTenant,
    _board_with_marker,
    _declarative_tenant,
    _mark_projection_migrated,
    _upgrade,
)

PROJECT = "porter"
ROLES = ("designer", "director", "audit", "ops", "app", "main")
ACCOUNTS = {f"{PROJECT}-{role}" for role in ROLES}

# Everything the upgrade prints once it believes the identities phase is behind
# it. None of these may follow a rolled-back one: each describes a step that is
# now the operator's or the director's, and reading any of them after a failure
# is reading the failure as progress.
CONTINUATION_MARKERS = (
    "the remaining step is the director's",
    "finish-upgrade",
    "no further deploy is needed",
    "withholding",
)


def _rolled_back_upgrade(tmp: Path, **kwargs):
    """A root upgrade refused before mutation because legacy panes are live."""
    config_path, _ = _declarative_tenant(tmp, accounts=True)
    _mark_projection_migrated(config_path)
    with _RunningTenant(config_path, account_uid=os.getuid() + 4242) as tenant:
        result, output, _migrations = _upgrade(
            config_path,
            as_root=True,
            exists=ACCOUNTS,
            runner=tenant.runner(),
            board=_board_with_marker(True),
            **kwargs,
        )
    return config_path, tenant, result, output


def test_a_rolled_back_cutover_fails_the_upgrade() -> None:
    """The project-account repatriation refusal reaches the upgrade caller."""
    with tempfile.TemporaryDirectory(prefix="cutover-propagation.") as tmp:
        config_path, tenant, result, output = _rolled_back_upgrade(Path(tmp))

        assert result != 0, output
        assert "refusing porter's project-account migration" in output, output
        assert "stop it at a resumable checkpoint before repatriation" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert all(role.get("run_as_user") == f"porter-{role['role']}" for role in after["roles"])
        assert tenant.stops == [] and tenant.launches == []
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert not journal.get("phases"), journal


def test_the_failed_upgrade_advertises_no_next_phase() -> None:
    """A nonzero exit under a continuation is still an operator told to proceed."""
    with tempfile.TemporaryDirectory(prefix="cutover-no-continuation.") as tmp:
        _config_path, _tenant, result, output = _rolled_back_upgrade(Path(tmp))

        assert result != 0, output
        for marker in CONTINUATION_MARKERS:
            assert marker not in output, (marker, output)
        assert "no account, worktree, installed unit, or release was changed" in output, output


def test_the_failed_upgrade_keeps_the_phase_report_and_the_recovery() -> None:
    """A pre-phase refusal reports only facts it actually established."""
    with tempfile.TemporaryDirectory(prefix="cutover-evidence.") as tmp:
        _config_path, _tenant, result, output = _rolled_back_upgrade(Path(tmp))

        assert result != 0, output
        assert "porter upgrade phases" not in output, output
        assert "rolling porter back" not in output, output
        assert "checkpoint before repatriation" in output, output


def test_a_completed_cutover_still_succeeds() -> None:
    """A checkpointed dedicated-account tenant repatriates successfully."""
    with tempfile.TemporaryDirectory(prefix="cutover-success.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        _mark_projection_migrated(config_path)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            tenant.live = False
            result, output, _migrations = _upgrade(
                config_path, as_root=True, exists=ACCOUNTS, runner=tenant.runner(),
                board=_board_with_marker(True),
            )
        assert result == 0, output
        assert "repatriated porter's resumable role state" in output, output
        assert "identities phase did not complete" not in output, output
        # The phases after it ran and were journaled, which is exactly what the
        # failing case must not do.
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") == "done", journal
        recorded = set(journal.get("phases") or {})
        assert {"release", "director"} <= recorded, recorded
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert after["role_state_isolation"] is True, after
        assert all("run_as_user" not in role for role in after["roles"]), after


def test_an_already_complete_cutover_still_succeeds() -> None:
    """A tenant already repatriated runs no migration a second time."""
    with tempfile.TemporaryDirectory(prefix="cutover-already-done.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        legacy = team_launcher.load_project_config(PROJECT, config_path)
        changed, problems = team_launcher.repatriate_role_runtime_state(
            legacy, config_path=config_path, runner=FakeRunner()
        )
        assert changed and not problems
        _mark_projection_migrated(config_path)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output, _migrations = _upgrade(
                config_path, as_root=True, exists=ACCOUNTS, runner=tenant.runner(),
                board=_board_with_marker(True),
            )
        # Nothing was stopped, because there was no transaction to run.
        assert result == 0, output
        assert tenant.stops == [], tenant.stops
        assert "rolling porter back" not in output, output
        assert "identities phase did not complete" not in output, output
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") == "done", journal


def test_a_dry_run_that_reaches_the_cutover_stays_zero() -> None:
    """The dry run reports repatriation and changes nothing."""
    with tempfile.TemporaryDirectory(prefix="cutover-dry-run.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        _mark_projection_migrated(config_path)
        before = config_path.read_bytes()
        # The accounts exist, so a real run would reach the transaction; this
        # one describes it instead. Without the accounts the upgrade stops at
        # the accounts phase and the propagation is never on the path at all.
        result, output, _migrations = _upgrade(
            config_path, as_root=True, exists=ACCOUNTS, dry_run=True,
        )
        assert result == 0, output
        assert "would repatriate porter's resumable role state" in output, output
        assert "identities phase did not complete" not in output, output
        assert config_path.read_bytes() == before
        assert not config_path.with_name("porter-upgrade.json").exists()


def test_a_dry_run_that_would_refuse_reports_nonzero() -> None:
    """A dry run still refuses while any legacy pane is live."""
    with tempfile.TemporaryDirectory(prefix="cutover-dry-run-refusal.") as tmp:
        config_path, _root = _declarative_tenant(Path(tmp), accounts=True)
        _mark_projection_migrated(config_path)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output, _migrations = _upgrade(
                config_path, as_root=True, exists=ACCOUNTS, dry_run=True,
                runner=tenant.runner(),
            )
        assert result != 0, output
        assert "checkpoint before repatriation" in output, output
        assert "would repatriate" not in output, output
        assert not config_path.with_name("porter-upgrade.json").exists()


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_cutover_failure_propagation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
