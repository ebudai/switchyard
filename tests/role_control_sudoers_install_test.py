#!/usr/bin/env python3
"""The generated role-control install must be a command that exists (SYRD-51).

Fresh provisioning, the upgrade/repair hand-off and add-role all install the same
`49-<project>-role-control` sudoers rule. Two of them did it by shelling out to
`switchyard provision <project> --render role-control-sudoers`, which the CLI has
no subcommand for, so privileged setup failed at that line and the director was
left without the control interface.

These tests therefore run the emitted lines instead of matching them. A stub
`sudo` on PATH executes what it is handed and the sudoers directory is a
temporary one -- the test cannot chown to root -- but the heredoc delivery, the
0440 mode, the real `visudo -c` on the staged file and the rename into place are
executed exactly as generated.
"""

from __future__ import annotations

import grp
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher  # noqa: E402
from scripts.ticket_board import project_provision as provision  # noqa: E402

ROLE_ACCOUNTS = (("director", "otto-director"), ("ops", "otto-ops"), ("app", "otto-app"))
OWNER = "otto-agent"


def _me() -> tuple[str, str]:
    return team_launcher.current_user_name(), grp.getgrgid(os.getgid()).gr_name


def _fragment(script: str, project: str) -> list[str]:
    """The role-control install, as generated, from its first line to the rename."""
    lines = script.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if "-o root -g root" in line and "role-control" in line
    )
    end = next(
        index
        for index, line in enumerate(lines[start:], start)
        if line.startswith("sudo mv") and f"49-{project}-role-control" in line
    )
    return lines[start : end + 1]


def _execute(fragment: list[str], *, project: str, workdir: Path) -> subprocess.CompletedProcess[str]:
    """Run the generated lines with sudo stubbed and the sudoers directory moved."""
    user, group = _me()
    sudoers_dir = workdir / "sudoers.d"
    sudoers_dir.mkdir(parents=True, exist_ok=True)
    bin_dir = workdir / "bin"
    bin_dir.mkdir(exist_ok=True)
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)

    body = "\n".join(fragment)
    body = body.replace("/etc/sudoers.d", str(sudoers_dir))
    # The only substitution that changes what the line does: an unprivileged test
    # cannot install a file owned by root.
    body = body.replace("-o root -g root", f"-o {user} -g {group}")
    script = "set -euo pipefail\n" + body + "\n"
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(workdir),
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )


def _installed(workdir: Path, project: str) -> Path:
    return workdir / "sudoers.d" / f"49-{project}-role-control"


def _assert_installed(workdir: Path, project: str, expected: str) -> None:
    installed = _installed(workdir, project)
    assert installed.is_file(), sorted(p.name for p in (workdir / "sudoers.d").iterdir())
    assert installed.read_text(encoding="utf-8") == expected
    assert stat.S_IMODE(installed.stat().st_mode) == 0o440, oct(installed.stat().st_mode)
    # The staged name is gone: it was renamed, not copied.
    assert not installed.with_suffix(".staged").exists()
    assert not (workdir / "sudoers.d" / f"49-{project}-role-control.staged").exists()
    # And what landed is a document sudo would actually load.
    check = subprocess.run(
        ["visudo", "-c", "-f", str(installed)], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stdout + check.stderr


# --- the three artifacts -----------------------------------------------------


def test_the_add_role_artifact_installs_the_control_interface_it_carries() -> None:
    commands = provision.role_account_commands(
        "otto",
        "app",
        OWNER,
        "boardsvc",
        role_accounts=ROLE_ACCOUNTS,
        worktree="/home/otto-agent/otto-worktrees/app",
    )
    expected = provision.render_role_control_sudoers("otto", OWNER, ROLE_ACCOUNTS)
    with tempfile.TemporaryDirectory(prefix="syrd51-add-role.") as tmp:
        workdir = Path(tmp)
        result = _execute(_fragment(commands, "otto"), project="otto", workdir=workdir)
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_installed(workdir, "otto", expected)
    # The grant for the role being added is in the document it installs.
    assert "otto-app" in expected


def test_the_upgrade_hand_off_installs_the_document_for_the_whole_role_set() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd51-upgrade.") as tmp:
        workdir = Path(tmp)
        config_path = _tenant_config(workdir, "porter")
        config = team_launcher.load_project_config("porter", config_path)
        artifact = team_launcher.render_role_account_migration(config)
        expected = provision.render_role_control_sudoers(
            "porter",
            config.run_as_user or team_launcher.current_user_name(),
            team_launcher.role_control_accounts(config),
        )

        result = _execute(_fragment(artifact, "porter"), project="porter", workdir=workdir)
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_installed(workdir, "porter", expected)
        # Every role the same artifact creates an account for is granted on.
        for role in config.roles:
            assert f"porter-{role.role}" in expected, role.role


def test_fresh_provisioning_installs_the_document_it_writes() -> None:
    plan = provision.build_plan(
        project="otto",
        owner_user=OWNER,
        owner_home=Path("/home") / OWNER,
    )
    with tempfile.TemporaryDirectory(prefix="syrd51-fresh.") as tmp:
        workdir = Path(tmp)
        provision.write_artifacts(plan, workdir, enable_owner_linger=False)
        commands = (workdir / "operator-commands.sh").read_text(encoding="utf-8")
        expected = (workdir / plan.role_control_sudoers_name).read_text(encoding="utf-8")
        assert expected.strip(), "this project has role accounts, so it has a control interface"

        result = _execute(_fragment(commands, "otto"), project="otto", workdir=workdir)
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_installed(workdir, "otto", expected)


# --- the failure this ticket is about ----------------------------------------


def _invoked_subcommands(script: str) -> list[str]:
    """Subcommands the script actually runs, read the way a shell reads them.

    Prose is not an invocation: `operator-commands.sh` says "switchyard refuses"
    in a comment, and the sudoers document names switchyard-publish-ref by path.
    Only a `switchyard` command word followed by a non-option word counts.
    """
    found: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped, comments=True)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            if token != "switchyard" and not token.endswith("/switchyard"):
                continue
            for candidate in tokens[index + 1 :]:
                if candidate.startswith("-"):
                    continue
                found.append(candidate)
                break
    return found


def test_no_generated_artifact_invokes_a_command_the_cli_does_not_have() -> None:
    """The root cause: `switchyard provision` is not a subcommand and never was."""
    assert "provision" not in team_launcher.SWITCHYARD_COMMANDS

    with tempfile.TemporaryDirectory(prefix="syrd51-commands.") as tmp:
        workdir = Path(tmp)
        config_path = _tenant_config(workdir, "porter")
        config = team_launcher.load_project_config("porter", config_path)
        plan = provision.build_plan(
            project="otto", owner_user=OWNER, owner_home=Path("/home") / OWNER
        )
        provision.write_artifacts(plan, workdir, enable_owner_linger=False)
        artifacts = {
            "add-role": provision.role_account_commands(
                "otto", "app", OWNER, "boardsvc", role_accounts=ROLE_ACCOUNTS
            ),
            "upgrade": team_launcher.render_role_account_migration(config),
            "fresh": (workdir / "operator-commands.sh").read_text(encoding="utf-8"),
        }

    for name, artifact in artifacts.items():
        unknown = sorted(set(_invoked_subcommands(artifact)) - set(team_launcher.SWITCHYARD_COMMANDS))
        assert unknown == [], f"{name} invokes {unknown}, which the CLI does not have"
    # A reader that finds nothing would pass this test forever: it finds the line
    # that caused this ticket, and the artifacts that legitimately drive the CLI.
    assert _invoked_subcommands(
        "switchyard provision otto --render role-control-sudoers"
    ) == ["provision"]
    assert "upgrade" in _invoked_subcommands(artifacts["upgrade"])
    # Fresh provisioning drives helpers by absolute path and names no subcommand,
    # so an empty reading there is the truth rather than a matcher that missed.
    assert _invoked_subcommands(artifacts["fresh"]) == []


def test_visudo_stops_a_broken_document_before_it_takes_the_loaded_name() -> None:
    """The check is load-bearing, not decoration: a bad file must not be installed.

    A malformed file under /etc/sudoers.d can lock a host out of sudo entirely,
    so the generated order -- stage, validate, rename -- has to fail closed.
    """
    broken = provision.role_control_sudoers_install_commands(
        "otto", "this is not a sudoers rule\n"
    )
    with tempfile.TemporaryDirectory(prefix="syrd51-broken.") as tmp:
        workdir = Path(tmp)
        result = _execute(broken, project="otto", workdir=workdir)
        assert result.returncode != 0, result.stdout
        assert not _installed(workdir, "otto").exists()
        # It stopped at the staged name, which sudo does not load: sudo ignores
        # any file in sudoers.d whose name contains a dot.
        assert (workdir / "sudoers.d" / "49-otto-role-control.staged").is_file()


def test_a_partial_role_set_is_refused_rather_than_installed() -> None:
    """The file is installed whole, so a partial document revokes what it omits."""
    assert provision.role_control_sudoers_install_commands("otto", "") == []
    assert provision.role_control_sudoers_install_commands("otto", "   \n") == []
    assert provision.render_role_control_sudoers("otto", OWNER, ()) == ""
    # A caller cannot forget the role set and silently get one role's document.
    try:
        provision.role_account_commands("otto", "app", OWNER, "boardsvc")
    except TypeError as exc:
        assert "role_accounts" in str(exc), exc
    else:
        raise AssertionError("role_account_commands accepted no role set")


def test_the_document_does_not_depend_on_a_writable_staging_path() -> None:
    """It used to stage through /tmp, which anything on the host can replace."""
    commands = provision.role_account_commands(
        "otto", "app", OWNER, "boardsvc", role_accounts=ROLE_ACCOUNTS
    )
    fragment = "\n".join(_fragment(commands, "otto"))
    assert "/tmp/" not in fragment, fragment
    assert fragment.splitlines()[0].endswith(f"<<'{provision.ROLE_CONTROL_SUDOERS_HEREDOC}'")


def _tenant_config(workdir: Path, project: str) -> Path:
    """A launcher configuration with several roles and no accounts yet."""
    roles = ["director", "audit", "ops", "app", "main"]
    layout = workdir / f"{project}-layout.json"
    layout.write_text(json.dumps({"KonsoleTabs": []}) + "\n", encoding="utf-8")
    config_path = workdir / f"{project}.json"
    config_path.write_text(
        json.dumps(
            {
                "project": project,
                "layout": str(layout),
                "run_as_user": team_launcher.current_user_name(),
                "roles": [
                    {
                        "role": role,
                        "target": f"{project}-{role}:0.0",
                        "tmux_session": f"{project}-{role}",
                        "cli": ["claude"],
                        "live_commands": ["claude"],
                        "workdir": str(workdir / "worktrees" / role),
                        "slot": index,
                    }
                    for index, role in enumerate(roles)
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_both_grants_survive_together_in_both_artifacts() -> None:
    """SYRD-50's lifecycle bridge and this role-control document are separate files.

    They are emitted next to each other by the same two artifacts, so a refresh
    that keeps one and drops the other is exactly the mistake to guard against.
    A recorded control user is forced here because a tenant whose only caller is
    its own account deliberately gets no bridge at all.
    """
    add_role = provision.role_account_commands(
        "otto", "app", OWNER, "boardsvc", role_accounts=ROLE_ACCOUNTS
    )
    assert "49-otto-role-control" in add_role, add_role
    assert "49-otto-tenant-control" in add_role, add_role
    assert add_role.index("49-otto-role-control") < add_role.index("49-otto-tenant-control")
    assert "switchyard-tenant-control" in add_role

    original = team_launcher.invoking_human
    with tempfile.TemporaryDirectory(prefix="syrd51-both-grants.") as tmp:
        workdir = Path(tmp)
        config_path = _tenant_config(workdir, "porter")
        config = team_launcher.load_project_config("porter", config_path)
        try:
            team_launcher.invoking_human = lambda: "eric"
            artifact = team_launcher.render_role_account_migration(config)
        finally:
            team_launcher.invoking_human = original

    assert "49-porter-role-control" in artifact, artifact
    assert "49-porter-tenant-control" in artifact, artifact
    assert artifact.index("49-porter-role-control") < artifact.index("49-porter-tenant-control")
    assert "eric" in artifact
    # And each is still installed the safe way, on its own file.
    assert artifact.count("visudo -c -f") == 2, artifact


def main() -> int:
    run_module_tests(globals())
    print("role_control_sudoers_install_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
