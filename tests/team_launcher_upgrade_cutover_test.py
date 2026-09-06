#!/usr/bin/env python3
"""SYRD-45: a tenant upgrade must be ordered, journaled and identity-safe.

The rollout this covers rewrote a declarative tenant's configuration to name
per-role Unix accounts before those accounts existed, then tried to make a
director-authority board write from the root upgrade process. The first leaves
roles that cannot start and panes the board would stop recognising; the second
cannot succeed without impersonating the director.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from team_launcher_test_helpers import *


def _declarative_tenant(tmp: Path, *, project: str = "porter", accounts: bool = False) -> tuple[Path, Path]:
    config_path = _write_six_visible_role_config(tmp, project=project)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    # A real account: the privileged paths chown generated files to the project
    # owner, and this suite runs the root branch.
    payload["run_as_user"] = team_launcher.current_user_name()
    payload["workflow"] = {"roles": [{"name": role["role"], "active": True} for role in payload["roles"]]}
    if accounts:
        for role in payload["roles"]:
            role["run_as_user"] = f"{project}-{role['role']}"
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path, tmp


class _AnyRoleSudoRunner(FakeRunner):
    """A tenant whose roles are running now, whichever account probes them.

    Once a role declares its own account every probe is wrapped in `sudo -u
    <account>`, so a runner that only understands bare tmux argv reports the
    project as stopped -- which is exactly the state in which a revert would be
    skipped.
    """

    def __call__(self, args, **kwargs):
        if args[:2] == ["sudo", "-u"] and len(args) > 3:
            inner = args[4:] if args[3] == "-H" else args[3:]
            return super().__call__(inner, **kwargs)
        return super().__call__(args, **kwargs)


def _live_runner(config_path: Path) -> FakeRunner:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    sessions = {role["tmux_session"] for role in payload["roles"]}
    commands = {role["target"]: role["cli"][0] for role in payload["roles"]}
    return _AnyRoleSudoRunner(existing_sessions=sessions, current_commands=commands)


def _upgrade(
    config_path: Path,
    *,
    as_root: bool,
    exists: set[str] | None = None,
    dry_run: bool = False,
    runner: FakeRunner | None = None,
):
    """Run the privileged upgrade with the host facts stated explicitly."""
    printed: list[str] = []
    known = exists if exists is not None else set()
    original_euid = team_launcher.os.geteuid
    original_exists = team_launcher.local_account_exists
    original_migrate = team_launcher.migrate_declarative_director_onboarding
    migrations: list[str] = []
    try:
        team_launcher.os.geteuid = (lambda: 0) if as_root else original_euid
        team_launcher.local_account_exists = lambda account: account in known
        team_launcher.migrate_declarative_director_onboarding = (
            lambda config, **kwargs: migrations.append(config.project) or True
        )
        config = team_launcher.load_project_config(
            json.loads(config_path.read_text(encoding="utf-8"))["project"], config_path
        )
        result = team_launcher.upgrade_project_command(
            config,
            config_path=config_path,
            dry_run=dry_run,
            runner=runner or FakeRunner(),
            print_func=printed.append,
        )
    finally:
        team_launcher.os.geteuid = original_euid
        team_launcher.local_account_exists = original_exists
        team_launcher.migrate_declarative_director_onboarding = original_migrate
    return result, "\n".join(printed), migrations


def test_root_upgrade_never_attempts_the_director_write() -> None:
    """No caller role exists for root, and manufacturing one would be impersonation."""
    with tempfile.TemporaryDirectory(prefix="upgrade-root-director.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        for name in ("TICKET_BOARD_CALLER_ROLE", "TICKET_BOARD_URL"):
            os.environ.pop(name, None)
        result, output, migrations = _upgrade(config_path, as_root=True)
        assert result == 0, output
        assert migrations == [], migrations
        assert "caller_role" not in output, output
        assert "finish-upgrade porter" in output, output
        assert "root cannot make that write" in output, output


def test_the_director_write_refuses_root_and_records_the_director() -> None:
    with tempfile.TemporaryDirectory(prefix="director-phase.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        config = team_launcher.load_project_config("porter", config_path)
        original_euid = team_launcher.os.geteuid
        printed: list[str] = []
        try:
            team_launcher.os.geteuid = lambda: 0
            try:
                team_launcher.migrate_declarative_director_onboarding(config, config_path=config_path)
                raise AssertionError("root was allowed to make the director's board write")
            except SystemExit as exc:
                assert "cannot be made by root" in str(exc), exc
            assert team_launcher.finish_upgrade_command(
                config, config_path=config_path, print_func=printed.append
            ) == 1
            assert "must not run as root" in "\n".join(printed), printed
        finally:
            team_launcher.os.geteuid = original_euid

        # The director's own process performs it and the journal records who.
        printed.clear()
        original_migrate = team_launcher.migrate_declarative_director_onboarding
        try:
            team_launcher.migrate_declarative_director_onboarding = lambda config, **kwargs: True
            assert team_launcher.finish_upgrade_command(
                config, config_path=config_path, runner=FakeRunner(), print_func=printed.append
            ) == 0
        finally:
            team_launcher.migrate_declarative_director_onboarding = original_migrate
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "director") == "done", journal
        assert team_launcher.current_user_name() in journal["phases"]["director"]["detail"]


def test_the_observed_partial_state_is_detected_and_reverted() -> None:
    """Config names per-role accounts, none exist, sessions still share the owner."""
    with tempfile.TemporaryDirectory(prefix="upgrade-partial.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        before = json.loads(config_path.read_text(encoding="utf-8"))
        assert all(role.get("run_as_user") for role in before["roles"])

        result, output, _migrations = _upgrade(
            config_path, as_root=True, exists=set(), runner=_live_runner(config_path)
        )
        assert result == 0, output
        assert "do not agree" in output, output
        assert "porter-designer does not exist" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert not any(role.get("run_as_user") for role in after["roles"]), after
        config = team_launcher.load_project_config("porter", config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") in {"reverted", "pending"}, journal


def test_a_freshly_provisioned_project_is_left_as_provisioned() -> None:
    """Nothing is running, so there is no legacy identity to preserve or undo."""
    with tempfile.TemporaryDirectory(prefix="upgrade-fresh.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        before = json.loads(config_path.read_text(encoding="utf-8"))
        result, output, _m = _upgrade(config_path, as_root=True, exists=set(), runner=FakeRunner())
        assert result == 0, output
        assert "left as provisioned" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert [role.get("run_as_user") for role in after["roles"]] == [
            role.get("run_as_user") for role in before["roles"]
        ], after


def test_the_cutover_happens_only_once_every_account_exists() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-cutover.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]

        # Nothing is written into the configuration while the accounts are absent.
        _result, output, _m = _upgrade(config_path, as_root=True, exists=set())
        assert "still share the project account" in output, output
        assert not any(
            role.get("run_as_user")
            for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]
        )
        assert (config_path.with_name("porter-role-accounts.sh")).is_file()

        # Once they exist, the same command cuts over.
        accounts = {f"porter-{role}" for role in roles}
        _result, output, _m = _upgrade(config_path, as_root=True, exists=accounts)
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert all(role.get("run_as_user") == f"porter-{role['role']}" for role in after["roles"]), after


def test_the_deploy_instruction_is_withheld_until_every_phase_is_ready() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-deploy-gate.") as tmp:
        (Path(tmp) / "partial").mkdir()
        config_path, _ = _declarative_tenant(Path(tmp))
        # A tenant that never opted into per-role accounts is consistent as it
        # stands, so only the outstanding director phase withholds the deploy.
        _result, output, _m = _upgrade(config_path, as_root=True, exists=set())
        assert "withholding" in output, output
        assert "director phase has not run" in output, output
        assert "deploy-restart" not in output, output

        # A tenant part-way onto per-role accounts withholds it for that too:
        # installing the generated unit then would authorize accounts nothing
        # runs as.
        partial_path, _ = _declarative_tenant(Path(tmp) / "partial", accounts=True)
        _result, output, _m = _upgrade(
            partial_path, as_root=True, exists={"porter-designer"}, runner=_live_runner(partial_path)
        )
        assert "do not all exist" in output or "do not agree" in output, output
        assert "deploy-restart" not in output, output


def test_dry_run_reports_every_phase_and_changes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-dry-run.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        before = config_path.read_bytes()
        result, output, _m = _upgrade(config_path, as_root=True, exists=set(), dry_run=True)
        assert result == 0, output
        assert config_path.read_bytes() == before
        assert not config_path.with_name("porter-upgrade.json").exists()
        for phase, owner, _detail in team_launcher.UPGRADE_PHASES:
            assert phase in output and owner in output, (phase, output)


def test_an_interrupted_upgrade_resumes_and_is_safe_to_retry() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-retry.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
        accounts = {f"porter-{role}" for role in roles}

        _result, _output, _m = _upgrade(config_path, as_root=True, exists=set())
        first = config_path.read_bytes()
        # Interrupted before the operator created the accounts: rerunning
        # changes nothing and repeats no instruction destructively.
        _result, _output, _m = _upgrade(config_path, as_root=True, exists=set())
        assert config_path.read_bytes() == first

        _result, _output, _m = _upgrade(config_path, as_root=True, exists=accounts)
        cut = config_path.read_bytes()
        _result, output, _m = _upgrade(config_path, as_root=True, exists=accounts)
        assert config_path.read_bytes() == cut, output


def test_each_tenant_carries_its_own_director_phase() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-multi-tenant.") as tmp:
        tmp_path = Path(tmp)
        first_dir = tmp_path / "porter"
        second_dir = tmp_path / "atlas"
        first_dir.mkdir()
        second_dir.mkdir()
        first_path, _ = _declarative_tenant(first_dir, project="porter")
        second_path, _ = _declarative_tenant(second_dir, project="atlas")
        migrated: list[str] = []
        original_migrate = team_launcher.migrate_declarative_director_onboarding
        try:
            team_launcher.migrate_declarative_director_onboarding = (
                lambda config, **kwargs: migrated.append(config.project) or True
            )
            for path, project in ((first_path, "porter"), (second_path, "atlas")):
                config = team_launcher.load_project_config(project, path)
                assert team_launcher.finish_upgrade_command(
                    config, config_path=path, runner=FakeRunner(), print_func=lambda _t: None
                ) == 0
        finally:
            team_launcher.migrate_declarative_director_onboarding = original_migrate
        assert migrated == ["porter", "atlas"], migrated
        for path, project in ((first_path, "porter"), (second_path, "atlas")):
            config = team_launcher.load_project_config(project, path)
            journal = team_launcher.read_upgrade_journal(config, config_path=path)
            assert journal["project"] == project, journal
            assert team_launcher.upgrade_phase_state(journal, "director") == "done", journal


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_upgrade_cutover_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
