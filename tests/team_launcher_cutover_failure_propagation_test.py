#!/usr/bin/env python3
"""SYRD-64: a rolled-back identity cutover must fail the upgrade that ran it.

On the SYRD-19 retry against 872a9a29 the identities transaction did its job:
it detected that the roles had not come back under their own accounts, rolled
the trees, the configuration, the units and the workers back, reported what
recovered, and returned 1. `upgrade_project_command` put that 1 in
`cutover_result` and never looked at it again -- it carried on into the
director phase, the release phase and the "the remaining step is the
director's" continuation, and returned 0. The rollout above it only noticed
because an unrelated assertion about the board build failed much later.

These cases hold the exit status and the output together, because either one
alone is what made the incident survivable: a nonzero exit whose output still
advertises the next phase, or a truthful report that exits 0, both leave a
caller reading a failed upgrade as a successful one.
"""

from __future__ import annotations

import json
import os
import shutil
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
    """A root upgrade whose identity transaction fails and rolls back.

    The accounts all exist, so the transaction runs; the panes keep the shared
    uid, so the kernel says the roles never moved. That is the exact shape of
    the observed failure -- a transaction that worked, found a real problem,
    and undid itself.
    """
    config_path, _ = _declarative_tenant(tmp)
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
    """The reported defect: the transaction returned 1 and the upgrade returned 0."""
    with tempfile.TemporaryDirectory(prefix="cutover-propagation.") as tmp:
        config_path, tenant, result, output = _rolled_back_upgrade(Path(tmp))

        assert result != 0, output
        # The rollback really happened, so this is the propagation of a genuine
        # failure and not an upgrade that refused before touching anything.
        assert "rolling porter back" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert not any(role.get("run_as_user") for role in after["roles"]), after
        assert len(tenant.stops) == 2 and len(tenant.launches) == 2, (tenant.stops, tenant.launches)
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") == "rolled back", journal
        # No later phase ran: the journal carries no verdict about the release
        # or the director, because neither was reached.
        recorded = set(journal.get("phases") or {})
        assert recorded == {"artifacts", "accounts", "identities"}, recorded


def test_the_failed_upgrade_advertises_no_next_phase() -> None:
    """A nonzero exit under a continuation is still an operator told to proceed."""
    with tempfile.TemporaryDirectory(prefix="cutover-no-continuation.") as tmp:
        _config_path, _tenant, result, output = _rolled_back_upgrade(Path(tmp))

        assert result != 0, output
        for marker in CONTINUATION_MARKERS:
            assert marker not in output, (marker, output)
        # And it says why it stopped, rather than simply ending.
        assert "identities phase did not complete" in output, output


def test_the_failed_upgrade_keeps_the_phase_report_and_the_recovery() -> None:
    """The evidence is the point of the report; propagating must not cost it."""
    with tempfile.TemporaryDirectory(prefix="cutover-evidence.") as tmp:
        _config_path, _tenant, result, output = _rolled_back_upgrade(Path(tmp))

        assert result != 0, output
        # Every phase line, with the identities phase carrying the state the
        # journal recorded rather than a default.
        assert "porter upgrade phases" in output, output
        for phase, owner, _detail in team_launcher.UPGRADE_PHASES:
            assert phase in output and owner in output, (phase, output)
        assert "identities" in output and "rolled back" in output, output
        # What the transaction printed about the recovery survives above it.
        assert "after the rollback" in output, output


def test_a_completed_cutover_still_succeeds() -> None:
    """Propagation must key on the transaction's own result, not on running it."""
    with tempfile.TemporaryDirectory(prefix="cutover-success.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        _mark_projection_migrated(config_path)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output, _migrations = _upgrade(
                config_path, as_root=True, exists=ACCOUNTS, runner=tenant.runner(),
                board=_board_with_marker(True),
            )
        assert result == 0, output
        assert "running under per-role identities" in output, output
        assert "identities phase did not complete" not in output, output
        # The phases after it ran and were journaled, which is exactly what the
        # failing case must not do.
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") == "done", journal
        recorded = set(journal.get("phases") or {})
        assert {"release", "director"} <= recorded, recorded
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert all(role["run_as_user"] == f"porter-{role['role']}" for role in after["roles"]), after


def test_an_already_complete_cutover_still_succeeds() -> None:
    """A tenant already on its accounts runs no transaction and owes no failure."""
    with tempfile.TemporaryDirectory(prefix="cutover-already-done.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
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
    """The dry run reports the transaction it would make and changes nothing."""
    with tempfile.TemporaryDirectory(prefix="cutover-dry-run.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        _mark_projection_migrated(config_path)
        before = config_path.read_bytes()
        # The accounts exist, so a real run would reach the transaction; this
        # one describes it instead. Without the accounts the upgrade stops at
        # the accounts phase and the propagation is never on the path at all.
        result, output, _migrations = _upgrade(
            config_path, as_root=True, exists=ACCOUNTS, dry_run=True,
        )
        assert result == 0, output
        assert "would stop" in output, output
        assert "identities phase did not complete" not in output, output
        assert config_path.read_bytes() == before
        assert not config_path.with_name("porter-upgrade.json").exists()


def test_a_dry_run_that_would_refuse_reports_nonzero() -> None:
    """A dry run exists to be believed, so its refusal is a refusal too.

    The staged role tooling is gone, which the transaction checks before it
    describes anything -- so this is a dry run that reports it would not make
    the cutover at all. Propagation that is conditional on `not dry_run` would
    hand that back as a successful rehearsal.
    """
    with tempfile.TemporaryDirectory(prefix="cutover-dry-run-refusal.") as tmp:
        config_path, root = _declarative_tenant(Path(tmp))
        _mark_projection_migrated(config_path)
        shutil.rmtree(root / "tooling")
        result, output, _migrations = _upgrade(
            config_path, as_root=True, exists=ACCOUNTS, dry_run=True,
        )
        assert result != 0, output
        assert "would stop" not in output, output
        assert "identities phase did not complete" in output, output
        # A refusal is not a rollback: nothing was stopped, so the report says
        # so rather than inventing a recovery.
        assert "Nothing was stopped" in output, output
        # And a dry run still writes no journal, so the report it prints comes
        # from the phases as they actually stand.
        assert not config_path.with_name("porter-upgrade.json").exists()


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_cutover_failure_propagation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
