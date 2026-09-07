#!/usr/bin/env python3
"""A wedged owner user manager must stop the upgrade, not hang it (SYRD-54).

A tenant owner's `systemd --user` was found spinning at 97% of a core, unable to
answer any request. Every `systemctl --user` call the launcher makes went to that
manager with no timeout, so the identities transaction -- which stops the roles,
then talks to the manager to take the listener down -- would have hung there with
the roles already stopped.

So the upgrade asks the manager whether it can answer before it does anything,
recovers it as root when it cannot, and refuses to start otherwise.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *  # noqa: F401,F403,E402
import team_launcher_upgrade_cutover_test as cutover  # noqa: E402

OWNER = team_launcher.current_user_name()
OWNER_UID = pwd.getpwnam(OWNER).pw_uid
MANAGER_UNIT = f"user@{OWNER_UID}.service"


class _Host:
    """A host whose user manager answers, hangs, or comes back after a restart.

    `systemctl --user` reaches the manager; a wedged one returns nothing at all,
    which is a timeout in the caller, so that is what this raises.
    """

    def __init__(self, *, wedged: bool, recovers: bool = True, listener: str = "active"):
        self.wedged = wedged
        self.recovers = recovers
        self.listener = listener
        self.user_calls: list[str] = []
        self.restarts: list[list[str]] = []
        self.other: list[list[str]] = []

    def __call__(self, args, **kwargs):
        argv = [str(part) for part in args]
        joined = " ".join(argv)
        if "systemctl --user" in joined:
            self.user_calls.append(joined)
            if self.wedged:
                # A wedged manager answers nothing, so an unbounded caller waits
                # forever. A test cannot wait forever, so it fails here instead:
                # the deadlock is the defect, not the timeout.
                if "timeout" not in kwargs or not kwargs["timeout"]:
                    raise AssertionError(f"unbounded call to a wedged user manager: {joined}")
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if "is-system-running" in joined:
                return subprocess.CompletedProcess(argv, 0, "running\n", "")
            if "is-active" in joined:
                return subprocess.CompletedProcess(argv, 0, f"{self.listener}\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["systemctl", "restart"]:
            self.restarts.append(argv)
            # The restart is what unwedges it, exactly as on the host.
            self.wedged = not self.recovers
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.other.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _tenant(tmp: Path) -> tuple[Path, "team_launcher.ProjectConfig"]:
    config_path, _ = cutover._declarative_tenant(tmp)
    return config_path, team_launcher.load_project_config("porter", config_path)


def _upgrade(config_path: Path, host: _Host, *, as_root: bool, dry_run: bool = False):
    printed: list[str] = []
    original_euid = team_launcher.os.geteuid
    original_exists = team_launcher.local_account_exists
    original_opener = team_launcher._open_board_url
    try:
        team_launcher.os.geteuid = (lambda: 0) if as_root else original_euid
        team_launcher.local_account_exists = lambda account: False
        team_launcher._open_board_url = cutover._unreachable_board
        config = team_launcher.load_project_config("porter", config_path)
        result = team_launcher.upgrade_project_command(
            config,
            config_path=config_path,
            dry_run=dry_run,
            # The tenant fixture stages a real bundle here (SYRD-62).
            tooling_root=config_path.parent / "tooling",
            runner=host,
            print_func=printed.append,
        )
    finally:
        team_launcher.os.geteuid = original_euid
        team_launcher.local_account_exists = original_exists
        team_launcher._open_board_url = original_opener
    return result, "\n".join(printed)


# --- reading the manager ------------------------------------------------------


def test_a_manager_that_never_answers_reads_as_wedged_not_as_inactive() -> None:
    with tempfile.TemporaryDirectory(prefix="wedged-state.") as tmp:
        config_path, config = _tenant(Path(tmp))
        host = _Host(wedged=True)
        state, detail = team_launcher.owner_user_manager_state(
            config, runner=host, config_path=config_path
        )
        assert state == team_launcher.MANAGER_WEDGED, (state, detail)
        # And the listener state is not guessed from silence.
        assert team_launcher.capture_listener_state(
            config, runner=host, config_path=config_path
        ) == team_launcher.MANAGER_WEDGED
        # It is asked of the manager itself, not of a unit.
        assert any("is-system-running" in call for call in host.user_calls), host.user_calls


def test_a_manager_that_answers_reads_as_responding() -> None:
    with tempfile.TemporaryDirectory(prefix="responding-state.") as tmp:
        config_path, config = _tenant(Path(tmp))
        host = _Host(wedged=False)
        state, _detail = team_launcher.owner_user_manager_state(
            config, runner=host, config_path=config_path
        )
        assert state == team_launcher.MANAGER_RESPONDING


def test_stopping_the_listener_fails_instead_of_hanging() -> None:
    """The call inside the transaction, at the point where hanging is worst."""
    with tempfile.TemporaryDirectory(prefix="wedged-stop.") as tmp:
        config_path, config = _tenant(Path(tmp))
        host = _Host(wedged=True)
        problems = team_launcher.stop_owner_listener(
            config, runner=host, config_path=config_path
        )
        assert problems, "a stop against a wedged manager must be a failure"
        assert "not answering" in problems[0], problems
        # It returned at all: the fake refuses to answer an unbounded call.
        assert host.user_calls


# --- what the upgrade does about it -------------------------------------------


def test_an_unprivileged_upgrade_stops_before_any_phase_runs() -> None:
    with tempfile.TemporaryDirectory(prefix="wedged-unprivileged.") as tmp:
        config_path, _config = _tenant(Path(tmp))
        before = config_path.read_bytes()
        host = _Host(wedged=True)

        result, output = _upgrade(config_path, host, as_root=False)

        assert result == 1, output
        assert "user manager is not answering" in output, output
        assert MANAGER_UNIT in output, output
        assert "sudo switchyard upgrade porter" in output, output
        assert "stopping before any phase runs" in output, output
        # Nothing was attempted: no restart, no configuration rewrite, no journal.
        assert host.restarts == [], host.restarts
        assert config_path.read_bytes() == before
        assert not config_path.with_name("porter-upgrade.json").exists()


def test_root_restarts_the_manager_and_proves_the_listener_returned() -> None:
    with tempfile.TemporaryDirectory(prefix="wedged-root.") as tmp:
        config_path, _config = _tenant(Path(tmp))
        host = _Host(wedged=True)

        result, output = _upgrade(config_path, host, as_root=True)

        assert host.restarts == [["systemctl", "restart", MANAGER_UNIT]], host.restarts
        assert "answering again" in output, output
        assert "porter-ticket-board-notify-listener.service is active again" in output, output
        # Recovery is not the end of the upgrade: it goes on to the ordered phases.
        assert result == 0, output
        assert "upgrade phases" in output, output
        # The listener was restarted and read back, not assumed.
        assert any("restart" in call for call in host.user_calls), host.user_calls
        assert any("is-active" in call for call in host.user_calls), host.user_calls


def test_a_restart_that_does_not_recover_it_stops_the_upgrade() -> None:
    with tempfile.TemporaryDirectory(prefix="wedged-stubborn.") as tmp:
        config_path, _config = _tenant(Path(tmp))
        before = config_path.read_bytes()
        host = _Host(wedged=True, recovers=False)

        # A manager that comes back takes seconds, so the real wait is long. This
        # case is about one that does not, and waiting out the real timeout would
        # prove nothing this shorter one does not.
        original_wait = team_launcher.OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS
        try:
            team_launcher.OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS = 2.0
            result, output = _upgrade(config_path, host, as_root=True)
        finally:
            team_launcher.OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS = original_wait

        assert result == 1, output
        assert "still does not answer" in output, output
        assert config_path.read_bytes() == before


def test_a_listener_that_does_not_come_back_is_reported_not_assumed() -> None:
    with tempfile.TemporaryDirectory(prefix="wedged-listener.") as tmp:
        config_path, _config = _tenant(Path(tmp))
        host = _Host(wedged=True, listener="failed")

        result, output = _upgrade(config_path, host, as_root=True)

        assert result == 1, output
        assert "is failed after the user manager restart" in output, output


def test_a_dry_run_says_what_it_would_restart_and_restarts_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="wedged-dry.") as tmp:
        config_path, _config = _tenant(Path(tmp))
        host = _Host(wedged=True)

        result, output = _upgrade(config_path, host, as_root=True, dry_run=True)

        assert f"would restart {MANAGER_UNIT}" in output, output
        assert host.restarts == [], host.restarts
        assert result == 0, output


def test_a_responding_manager_is_left_alone() -> None:
    with tempfile.TemporaryDirectory(prefix="healthy.") as tmp:
        config_path, _config = _tenant(Path(tmp))
        host = _Host(wedged=False)

        result, output = _upgrade(config_path, host, as_root=True)

        assert host.restarts == [], host.restarts
        assert "user manager is not answering" not in output, output
        assert result == 0, output


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("owner_user_manager_recovery_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
