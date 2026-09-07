#!/usr/bin/env python3
"""SYRD-63: the reviewed unit goes in before the release deploy's drift gate.

The SYRD-19 cutover at 872a9a29 generated a board unit carrying
`SupplementaryGroups=<project>-roles`, the strict runtime directory mode and the
per-role identity table -- and then ran the release deploy before installing it.
`ticket-board-service.sh deploy-restart` compares the release's own production
unit with the one still installed, classified the difference as
`sensitive unit drift: SupplementaryGroups`, and refused, because daemon-reload
is deliberately outside the board's deploy grant. It was right to: with the old
unit installed, the authority migration this transaction *is* is
indistinguishable from operator drift. All six roles rolled back.

Installing the file and reloading systemd restarts nothing, so the invariant
that put the binary first still holds -- the old board is never restarted under
a unit its release cannot serve, because the restart comes with the new binary.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *
from team_launcher_upgrade_cutover_test import (
    _RunningTenant,
    _declarative_tenant,
    _deployed_release,
    _origin_backed_source,
)

PROJECT = "porter"
ROLES_GROUP = f"{PROJECT}-roles"
BOARD_UNIT = f"{PROJECT}-ticket-board.service"
#: What the deploy says when it refuses. The wording is the shell script's.
DRIFT_REFUSAL = (
    "candidate system unit differs from installed unit; daemon-reload is required but is "
    "intentionally outside the board deploy polkit grant"
)

OLD_UNIT = """[Service]
User=boardsvc
ExecStart=/bin/true
RuntimeDirectory=porter-ticket-board
"""
NEW_UNIT = f"""[Service]
User=boardsvc
SupplementaryGroups={ROLES_GROUP}
ExecStart=/bin/true
RuntimeDirectory=porter-ticket-board
RuntimeDirectoryMode=0750
Environment=TICKET_BOARD_ROLE_ACCOUNTS=main=porter-main
"""


# --------------------------------------------------------------------------
# The gate itself, driven from the script that owns it.
# --------------------------------------------------------------------------


def test_the_release_gate_calls_this_migration_sensitive_drift() -> None:
    """Not a claim about the deploy: its own classifier, on its own input.

    Everything below models a refusal, so this establishes that the refusal is
    real and that this exact migration is what provokes it.
    """
    with tempfile.TemporaryDirectory(prefix="drift-classify.") as raw:
        tmp = Path(raw)
        old, new, diff = tmp / "old", tmp / "new", tmp / "diff"
        old.write_text(OLD_UNIT, encoding="utf-8")
        new.write_text(NEW_UNIT, encoding="utf-8")
        probe = tmp / "probe.sh"
        probe.write_text(
            "set -euo pipefail\n"
            f"source {shlex.quote(str(ROOT / 'scripts' / 'ticket-board-service.sh'))}\n"
            f"diff -u {shlex.quote(str(old))} {shlex.quote(str(new))} > {shlex.quote(str(diff))} || true\n"
            f"classify_system_unit_drift {shlex.quote(str(diff))}\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(probe)],
            env={**os.environ, "TICKET_BOARD_PROJECT": PROJECT},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "sensitive unit drift: SupplementaryGroups", result.stdout
        # And an Environment-only change is not: the gate is not simply strict,
        # it is strict about the directives that decide identity.
        environment_only = tmp / "env-only"
        environment_only.write_text(
            OLD_UNIT + "Environment=TICKET_BOARD_ROLE_ACCOUNTS=main=porter-main\n", encoding="utf-8"
        )
        probe.write_text(
            "set -euo pipefail\n"
            f"source {shlex.quote(str(ROOT / 'scripts' / 'ticket-board-service.sh'))}\n"
            f"diff -u {shlex.quote(str(old))} {shlex.quote(str(environment_only))} "
            f"> {shlex.quote(str(diff))} || true\n"
            f"classify_system_unit_drift {shlex.quote(str(diff))}\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(probe)],
            env={**os.environ, "TICKET_BOARD_PROJECT": PROJECT},
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "Environment-only drift", result.stdout


# --------------------------------------------------------------------------
# The transaction, against a host that applies that gate.
# --------------------------------------------------------------------------


def _migrating_tenant(tmp: Path) -> tuple[Path, Path, Path, Path]:
    """A tenant mid-migration: old unit installed, new unit staged.

    The old one has no roles group, which is the whole reason the new one
    exists; nobody has installed the new one, which is the state an upgrade
    reaches on its own.
    """
    source_repo, _target = _origin_backed_source(tmp)
    board_root = _deployed_release(tmp, PROJECT, "1" * 40)
    config_path, _ = _declarative_tenant(tmp, board_root=board_root)
    staged = team_launcher.privileged_provision_dir(
        PROJECT, root=team_launcher.switchyard_privileged_provision_root()
    ) / BOARD_UNIT
    staged.write_text(NEW_UNIT, encoding="utf-8")
    installed = team_launcher.SYSTEMD_UNIT_DIR / BOARD_UNIT
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(OLD_UNIT, encoding="utf-8")
    return config_path, source_repo, board_root, installed


def _provisioned_unit(command: str) -> Path | None:
    """The unit the deploy was told is the release's own, as it reads it."""
    for word in shlex.split(command):
        if word.startswith("TICKET_BOARD_PROVISIONED_SYSTEM_UNIT="):
            return Path(word.split("=", 1)[1])
    return None


def _gated_host(
    inner,
    *,
    board_root: Path,
    source_repo: Path,
    installed: Path,
    calls: list[list[str]],
    deploys: list[str],
    deploy_fails: bool = False,
):
    """A host whose deploy applies the drift gate before it does anything.

    Modelled on `assert_system_unit_reload_not_required_for_release`: the
    release's own production unit is compared byte-for-byte with the installed
    one, and a difference is refused with the message that names daemon-reload.
    """

    def call(args, **kwargs):
        argv = [str(part) for part in args]
        calls.append(argv)
        bare = argv
        if bare[:2] == ["sudo", "-u"] and len(bare) > 3:
            # Root's own git reads are de-escalated to the owning account, so
            # the read arrives wrapped in sudo (SYRD-48).
            bare = bare[4:] if bare[3] == "-H" else bare[3:]
        if bare[:1] == ["git"] and ("ls-remote" in bare or "rev-parse" in bare):
            passthrough = dict(kwargs)
            passthrough.setdefault("text", True)
            passthrough.setdefault("stdout", subprocess.PIPE)
            passthrough.setdefault("stderr", subprocess.PIPE)
            return subprocess.run(bare, **passthrough)
        joined = " ".join(argv)
        if "deploy-restart" in joined:
            candidate = _provisioned_unit(joined)
            if candidate is None or not candidate.is_file():
                return subprocess.CompletedProcess(argv, 1, "", "no candidate system unit")
            if not installed.is_file() or candidate.read_bytes() != installed.read_bytes():
                return subprocess.CompletedProcess(argv, 1, "", DRIFT_REFUSAL)
            if deploy_fails:
                return subprocess.CompletedProcess(argv, 1, "", "the release would not build")
            deploys.append(joined)
            sha = _run_git(["git", "rev-parse", "origin/main"], cwd=source_repo).stdout.strip()
            release = board_root / "releases" / sha
            release.mkdir(parents=True, exist_ok=True)
            (release / ".pgu-deploy-sha").write_text(sha + "\n", encoding="utf-8")
            current = board_root / "current"
            if current.is_symlink() or current.exists():
                current.unlink()
            current.symlink_to(release)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return inner(argv, **kwargs)

    return call


def _cutover(config_path: Path, *, runner, source_repo: Path, **kwargs):
    printed: list[str] = []
    original_euid = team_launcher.os.geteuid
    try:
        team_launcher.os.geteuid = lambda: 0
        result = team_launcher.cutover_role_identities_command(
            team_launcher.load_project_config(PROJECT, config_path),
            config_path=config_path,
            source_repo=source_repo,
            tooling_dir=config_path.parent / "tooling" / PROJECT,
            runner=runner,
            print_func=printed.append,
            **kwargs,
        )
    finally:
        team_launcher.os.geteuid = original_euid
    return result, "\n".join(printed)


def _index_of(calls: list[list[str]], predicate) -> int:
    for index, argv in enumerate(calls):
        if predicate(argv):
            return index
    return -1


def test_the_cutover_installs_the_reviewed_unit_before_the_deploy_gate() -> None:
    """The whole incident: old unit installed, new unit staged, deploy gated."""
    with tempfile.TemporaryDirectory(prefix="authority-order.") as raw:
        tmp = Path(raw)
        config_path, source_repo, board_root, installed = _migrating_tenant(tmp)
        assert "SupplementaryGroups" not in installed.read_text(encoding="utf-8")
        calls: list[list[str]] = []
        deploys: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _cutover(
                config_path,
                runner=_gated_host(
                    tenant.runner(),
                    board_root=board_root,
                    source_repo=source_repo,
                    installed=installed,
                    calls=calls,
                    deploys=deploys,
                ),
                source_repo=source_repo,
            )
        assert result == 0, output
        assert deploys, output
        assert DRIFT_REFUSAL not in output, output
        # The reviewed unit is what is installed now, and nobody was asked to
        # install it by hand.
        assert installed.read_text(encoding="utf-8") == NEW_UNIT
        assert "Ask an operator" not in output and "install the candidate" not in output, output
        # And the order is the point: the file and the reload before the gate.
        install_at = _index_of(calls, lambda argv: argv[:1] == ["install"] and argv[-1] == str(installed))
        reload_at = _index_of(calls, lambda argv: argv[:2] == ["systemctl", "daemon-reload"])
        deploy_at = _index_of(calls, lambda argv: "deploy-restart" in " ".join(argv))
        assert -1 not in (install_at, reload_at, deploy_at), calls
        assert install_at < reload_at < deploy_at, (install_at, reload_at, deploy_at)


def test_the_board_is_restarted_once_and_only_when_the_deploy_did_not() -> None:
    """The deploy restarts and verifies far more than a bare restart can."""
    with tempfile.TemporaryDirectory(prefix="authority-restart.") as raw:
        tmp = Path(raw)
        config_path, source_repo, board_root, installed = _migrating_tenant(tmp)
        calls: list[list[str]] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _cutover(
                config_path,
                runner=_gated_host(
                    tenant.runner(),
                    board_root=board_root,
                    source_repo=source_repo,
                    installed=installed,
                    calls=calls,
                    deploys=[],
                ),
                source_repo=source_repo,
            )
        assert result == 0, output
        restarts = [argv for argv in calls if argv[:2] == ["systemctl", "restart"] and argv[-1] == BOARD_UNIT]
        assert restarts == [], restarts
        # It was still read back: installing a unit is not the board serving it.
        assert any(
            argv[:2] == ["systemctl", "is-active"] and argv[-1] == BOARD_UNIT for argv in calls
        ), calls


def test_a_tenant_with_no_release_to_deploy_still_restarts_the_board() -> None:
    """Nothing deployed means nothing restarted it, and the unit must take effect."""
    with tempfile.TemporaryDirectory(prefix="authority-norelease.") as raw:
        tmp = Path(raw)
        # No board root: this tenant serves its board from the shared install,
        # so the transaction has no release of its own to switch.
        config_path, _ = _declarative_tenant(tmp)
        staged = team_launcher.privileged_provision_dir(
            PROJECT, root=team_launcher.switchyard_privileged_provision_root()
        ) / BOARD_UNIT
        staged.write_text(NEW_UNIT, encoding="utf-8")
        installed = team_launcher.SYSTEMD_UNIT_DIR / BOARD_UNIT
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text(OLD_UNIT, encoding="utf-8")
        calls: list[list[str]] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            inner = tenant.runner()

            def record(args, **kwargs):
                calls.append([str(part) for part in args])
                return inner(args, **kwargs)

            result, output = _cutover(config_path, runner=record, source_repo=ROOT)
        assert result == 0, output
        assert installed.read_text(encoding="utf-8") == NEW_UNIT
        restarts = [argv for argv in calls if argv[:2] == ["systemctl", "restart"] and argv[-1] == BOARD_UNIT]
        assert len(restarts) == 1, restarts


def test_a_refused_deploy_puts_the_old_unit_and_everything_else_back() -> None:
    """The new unit is installed before the deploy, so a rollback has to undo it."""
    with tempfile.TemporaryDirectory(prefix="authority-rollback.") as raw:
        tmp = Path(raw)
        config_path, source_repo, board_root, installed = _migrating_tenant(tmp)
        before_config = config_path.read_bytes()
        before_release = os.readlink(board_root / "current")
        before_ownership = team_launcher._worktree_ownership(
            team_launcher.load_project_config(PROJECT, config_path)
        )
        calls: list[list[str]] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            tenant.listener_active = True
            result, output = _cutover(
                config_path,
                runner=_gated_host(
                    tenant.runner(),
                    board_root=board_root,
                    source_repo=source_repo,
                    installed=installed,
                    calls=calls,
                    deploys=[],
                    deploy_fails=True,
                ),
                source_repo=source_repo,
            )
        assert result == 1, output
        assert "the release would not build" in output, output
        # The unit this transaction installed is gone again.
        assert installed.read_text(encoding="utf-8") == OLD_UNIT
        assert config_path.read_bytes() == before_config
        assert os.readlink(board_root / "current") == before_release
        assert team_launcher._worktree_ownership(
            team_launcher.load_project_config(PROJECT, config_path)
        ) == before_ownership
        # The workers came back, the presentation was reconnected and the
        # listener is running again.
        assert len(tenant.launches) >= 1, tenant.launches
        assert tenant.listener_active is True, tenant.listener_calls
        assert "role session(s) are live" in output, output
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert journal["phases"]["identities"]["state"] == "rolled back", journal


def test_the_transaction_installs_what_the_operator_sequence_installs() -> None:
    """One migration, two ways of performing it; they cannot disagree.

    The transaction installed the board and listener units and not the canary,
    while the printed sequence installed all three -- and the deploy the
    transaction now runs starts the canary through systemd.
    """
    with tempfile.TemporaryDirectory(prefix="authority-parity.") as raw:
        tmp = Path(raw)
        config_path, source_repo, board_root, _installed = _migrating_tenant(tmp)
        config = team_launcher.load_project_config(PROJECT, config_path)
        status = team_launcher.tenant_release_status(
            config, config_path=config_path, source_repo=source_repo
        )
        assert status is not None and status.provisioned_system_unit is not None

        printed = team_launcher.tenant_release_unit_install_command(status, PROJECT)
        assert printed, "the operator sequence installs units"
        printed_units = {
            Path(word).name
            for word in shlex.split(printed.replace(" && ", " "))
            if word.endswith(".service")
        }
        performed = {
            unit for unit, _destination, _ownership in team_launcher.authority_unit_installs(
                config, config_path=config_path
            )
        }
        assert performed == printed_units, (performed, printed_units)
        # Both reload before anything is deployed, and the canary is in both.
        assert f"{PROJECT}-ticket-board-canary.service" in performed, performed
        assert printed.rstrip().endswith("systemctl daemon-reload"), printed


def test_the_printed_sequence_installs_before_it_deploys() -> None:
    """An operator following it must not meet the refusal either."""
    with tempfile.TemporaryDirectory(prefix="authority-printed.") as raw:
        tmp = Path(raw)
        config_path, source_repo, _board_root, _installed = _migrating_tenant(tmp)
        config = team_launcher.load_project_config(PROJECT, config_path)
        printed: list[str] = []
        team_launcher.report_tenant_release_upgrade(
            config,
            config_path=config_path,
            source_repo=source_repo,
            print_func=printed.append,
        )
        output = "\n".join(printed)
        install_at = output.index("systemctl daemon-reload")
        deploy_at = output.index("deploy-restart")
        assert install_at < deploy_at, output


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_authority_before_deploy_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
