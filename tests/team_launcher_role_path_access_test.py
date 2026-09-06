#!/usr/bin/env python3
"""SYRD-49: a role must reach its own work, and the director its own commands.

The cutover gives each role a distinct Unix account and chowns its worktree,
but the worktree lives beneath an owner home that is 0710, and a linked
worktree's gitdir points into the owner's control repository. Owning the leaf
is not reaching it. The director has the same problem from the other side: its
configuration is a 0600 file under that home, so the entrypoint escalated to
root and the command it was trying to run then refused root.

This runs in a user namespace where this process is root, with real distinct
accounts, the live path shape, and a 0710 owner home.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher
from scripts.ticket_board.project_provision import (
    director_control_access_commands,
    role_worktree_access_commands,
)


def _accounts(count: int) -> list[str]:
    seen: dict[int, str] = {}
    for entry in pwd.getpwall():
        if 0 < entry.pw_uid < 1000 and entry.pw_uid not in seen:
            seen[entry.pw_uid] = entry.pw_name
    names = [seen[uid] for uid in sorted(seen)][:count]
    assert len(names) == count, names
    return names


def _as(account: str, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    entry = pwd.getpwnam(account)
    return subprocess.run(
        [
            "setpriv",
            f"--reuid={entry.pw_uid}",
            f"--regid={entry.pw_gid}",
            "--clear-groups",
            *argv,
        ],
        capture_output=True,
        text=True,
        **kwargs,
    )


def _apply(commands: list[str]) -> None:
    for command in commands:
        # The artifact is written for an operator with sudo; here the process is
        # already root, so the same command runs without it.
        stripped = command.replace("sudo ", "", 1)
        result = subprocess.run(stripped, shell=True, capture_output=True, text=True)
        assert result.returncode == 0, (stripped, result.stderr)


def role_and_director_access(owner: str) -> None:
    assert os.geteuid() == 0
    role_account, director_account, impostor_account = _accounts(3)
    with tempfile.TemporaryDirectory(prefix="syrd49-") as tmp:
        tmp_path = Path(tmp)
        tmp_path.chmod(0o755)
        owner_entry = pwd.getpwnam(owner)

        # The live shape: a 0710 owner home with the worktrees, the control
        # repository and the provisioning directory all beneath it.
        home = tmp_path / "home" / owner
        home.mkdir(parents=True)
        worktree_base = home / "porter-worktrees"
        worktree = worktree_base / "main"
        worktree.mkdir(parents=True)
        control = home / ".local" / "state" / "switchyard" / "projects" / "porter" / "control.git"
        (control / "worktrees" / "main").mkdir(parents=True)
        (control / "objects").mkdir()
        (control / "worktrees" / "main" / "gitdir").write_text(str(worktree / ".git"), encoding="utf-8")
        (worktree / ".git").write_text(f"gitdir: {control / 'worktrees' / 'main'}\n", encoding="utf-8")
        credentials = home / ".claude"
        credentials.mkdir()
        (credentials / ".credentials.json").write_text('{"token": "owner-only"}', encoding="utf-8")
        credentials.chmod(0o700)
        (credentials / ".credentials.json").chmod(0o600)
        project_dir = home / "Projects" / "porter"
        provision = project_dir / ".switchyard" / "provision"
        provision.mkdir(parents=True)
        layout = provision / "porter-layout.json"
        layout.write_text(json.dumps({"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}), encoding="utf-8")
        config_path = provision / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "desktop_access": {"mode": "headless"},
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(project_dir),
                    "worktree_base": str(worktree_base),
                    "run_as_user": owner,
                    # A declarative tenant whose control role is NOT called
                    # "director": what makes it the control role is what the
                    # workflow lets it do. The role that IS called director here
                    # has no control capabilities and must not be mistaken for
                    # it (SYRD-49).
                    "workflow": {
                        "roles": [
                            {
                                "name": "conductor",
                                "active": True,
                                "capabilities": [
                                    "add_comment",
                                    "merge",
                                    "set_manually_controlled",
                                ],
                            },
                            {
                                "name": "director",
                                "active": True,
                                "capabilities": ["add_comment", "edit_fields"],
                            },
                            {"name": "main", "active": True, "capabilities": ["add_comment"]},
                        ]
                    },
                    "roles": [
                        {
                            "role": "main",
                            "slot": 0,
                            "cli": ["codex"],
                            "target": "porter-main:0.0",
                            "tmux_session": "porter-main",
                            "workdir": str(worktree),
                            "run_as_user": role_account,
                        },
                        {
                            "role": "conductor",
                            "slot": 1,
                            "cli": ["claude"],
                            "target": "porter-conductor:0.0",
                            "tmux_session": "porter-conductor",
                            "workdir": str(worktree_base / "conductor"),
                            "run_as_user": director_account,
                        },
                        {
                            "role": "director",
                            "slot": 2,
                            "cli": ["claude"],
                            "target": "porter-director:0.0",
                            "tmux_session": "porter-director",
                            "workdir": str(worktree_base / "director"),
                            "run_as_user": impostor_account,
                        },
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (worktree_base / "conductor").mkdir()
        (worktree_base / "director").mkdir()
        registry = tmp_path / "registry"
        registry.mkdir()
        (registry / "porter.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "name": "Porter",
                    "slug": "porter",
                    "config_path": str(config_path),
                }
            ),
            encoding="utf-8",
        )

        # Ownership as the cutover leaves it, and the access contract the
        # configuration starts with.
        for path in (tmp_path / "home", home, worktree_base, control, project_dir,
                     project_dir.parent, project_dir / ".switchyard", provision, credentials):
            os.chown(path, owner_entry.pw_uid, owner_entry.pw_gid)
        for path in (config_path, layout, credentials / ".credentials.json"):
            os.chown(path, owner_entry.pw_uid, owner_entry.pw_gid)
        for path in control.rglob("*"):
            os.chown(path, owner_entry.pw_uid, owner_entry.pw_gid)
        subprocess.run(["chown", "-R", f"{role_account}:", str(worktree)], check=True)
        subprocess.run(["chown", "-R", f"{director_account}:", str(worktree_base / "conductor")], check=True)
        subprocess.run(["chown", "-R", f"{impostor_account}:", str(worktree_base / "director")], check=True)
        home.chmod(0o710)
        config_path.chmod(0o600)

        probe = worktree / "file"
        # BEFORE: owning the leaf is not reaching it, and the director cannot
        # read its own configuration.
        assert _as(role_account, ["test", "-w", str(worktree)]).returncode != 0
        assert _as(director_account, ["cat", str(config_path)]).returncode != 0

        _apply(
            role_worktree_access_commands(
                owner_home=str(home),
                roles_group=pwd.getpwnam(role_account).pw_name,  # a group of one, for this case
                worktree_base=str(worktree_base),
                control_repository=str(control),
            )
        )
        _apply(
            director_control_access_commands(
                owner_home=str(home),
                director_account=director_account,
                provision_dir=str(provision),
                project_dir=str(project_dir),
            )
        )

        # AFTER: the role reaches its own tree and the shared git metadata.
        assert _as(role_account, ["sh", "-c", f"echo work > {probe}"]).returncode == 0, probe
        gitdir = control / "worktrees" / "main" / "HEAD"
        assert _as(role_account, ["sh", "-c", f"echo ref: refs/heads/x > {gitdir}"]).returncode == 0
        assert _as(role_account, ["sh", "-c", f"echo o > {control / 'objects' / 'probe'}"]).returncode == 0
        # And still cannot read the owner's credentials, or list the home.
        assert _as(role_account, ["cat", str(credentials / ".credentials.json")]).returncode != 0
        assert _as(role_account, ["ls", str(home)]).returncode != 0
        assert _as(director_account, ["cat", str(credentials / ".credentials.json")]).returncode != 0

        # The director can read AND replace its durable control artifacts, and
        # an atomic replacement keeps the contract.
        assert _as(director_account, ["cat", str(config_path)]).returncode == 0
        rewritten = provision / "porter.json.new"
        assert _as(
            director_account,
            ["sh", "-c", f"cp {config_path} {rewritten} && mv {rewritten} {config_path}"],
        ).returncode == 0
        assert _as(director_account, ["cat", str(config_path)]).returncode == 0

        # A readable copy of the entrypoint, as the shared install provides:
        # this checkout is under a home no role account may read.
        release = tmp_path / "release"
        release.mkdir()
        shutil.copy2(ROOT / "switchyard", release / "switchyard")
        shutil.copytree(ROOT / "scripts", release / "scripts")
        for path in release.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        (release / "switchyard").chmod(0o755)
        release.chmod(0o755)

        # The public entrypoint, run as the director: it must not escalate.
        sudo_stub = tmp_path / "bin"
        sudo_stub.mkdir()
        escalations = tmp_path / "escalations"
        # Records only an attempt to run the COMMAND as root; `sudo -n -v` is a
        # capability probe, not an escalation of the command itself.
        (sudo_stub / "sudo").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *switchyard*) echo \"$@\" >> " + str(escalations) + " ;;\n"
            "esac\n"
            "exit 1\n",
            encoding="utf-8",
        )
        (sudo_stub / "sudo").chmod(0o755)
        result = _as(
            director_account,
            [
                "env",
                f"PATH={sudo_stub}:/usr/local/sbin:/usr/local/bin:/usr/bin",
                f"HOME={worktree_base / 'conductor'}",
                f"SWITCHYARD_PROJECT_REGISTRY_DIR={registry}",
                sys.executable,
                str(release / "switchyard"),
                "finish-upgrade",
                "porter",
                "--dry-run",
            ],
        )
        assert not escalations.exists(), escalations.read_text()
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "would migrate" in result.stdout, result.stdout

        # The role literally named "director", which has no control
        # capabilities, is not the control role: its account is refused.
        impostor = _as(
            impostor_account,
            [
                "env",
                f"PATH={sudo_stub}:/usr/local/sbin:/usr/local/bin:/usr/bin",
                f"HOME={worktree_base / 'director'}",
                f"SWITCHYARD_PROJECT_REGISTRY_DIR={registry}",
                sys.executable,
                str(release / "switchyard"),
                "finish-upgrade",
                "porter",
            ],
        )
        assert impostor.returncode != 0, (impostor.stdout, impostor.stderr)
        assert not escalations.exists(), escalations.read_text()

        # A different role account is told so, rather than having its write
        # rejected by the board later.
        wrong = _as(
            role_account,
            [
                "env",
                f"PATH={sudo_stub}:/usr/local/sbin:/usr/local/bin:/usr/bin",
                f"HOME={worktree}",
                f"SWITCHYARD_PROJECT_REGISTRY_DIR={registry}",
                sys.executable,
                str(release / "switchyard"),
                "finish-upgrade",
                "porter",
            ],
        )
        # It cannot perform the director's phase, and it is not silently given
        # root to do it with either.
        assert wrong.returncode != 0, (wrong.stdout, wrong.stderr)
        assert "This process is" in wrong.stdout or "requires root" in wrong.stderr, (
            wrong.stdout, wrong.stderr
        )
        assert not escalations.exists(), escalations.read_text()

        # Rollback returns the worktree to the owner and widens nothing.
        subprocess.run(["chown", "-R", f"{owner}:", str(worktree)], check=True)
        assert _as(role_account, ["cat", str(credentials / ".credentials.json")]).returncode != 0
        assert _as(owner, ["sh", "-c", f"echo back > {probe}"]).returncode == 0


def test_containment_is_by_component_not_by_prefix() -> None:
    """/home/foobar starts with /home/foo, and is somebody else's home."""
    from scripts.ticket_board.project_provision import (
        PathContainmentError,
        director_control_access_commands,
        role_worktree_access_commands,
    )

    inside = role_worktree_access_commands(
        owner_home="/home/foo",
        roles_group="porter-roles",
        worktree_base="/home/foo/porter-worktrees",
        control_repository="/home/foo/.local/state/switchyard/projects/porter/control.git",
    )
    assert any("/home/foo/.local/state'" in command for command in inside), inside

    for builder in (
        lambda: role_worktree_access_commands(
            owner_home="/home/foo",
            roles_group="porter-roles",
            worktree_base="/home/foo/porter-worktrees",
            control_repository="/home/foobar/.local/state/porter/control.git",
        ),
        lambda: director_control_access_commands(
            owner_home="/home/foo",
            director_account="porter-conductor",
            provision_dir="/home/foobar/Projects/porter/.switchyard/provision",
            project_dir="/home/foobar/Projects/porter",
        ),
    ):
        try:
            builder()
        except PathContainmentError as exc:
            assert "/home/foobar" in str(exc), exc
            continue
        raise AssertionError("a path outside the owner home was granted access")

    # The sibling home is never named in a grant computed for the other.
    assert not any("foobar" in command for command in inside), inside

    # A path kept genuinely elsewhere is an ordinary configuration, not a
    # coincidence: it needs no grant on the owner's home at all.
    elsewhere = director_control_access_commands(
        owner_home="/home/foo",
        director_account="porter-conductor",
        provision_dir="/srv/porter/.switchyard/provision",
        project_dir="/srv/porter",
    )
    assert not any("/home/foo'" in command for command in elsewhere), elsewhere
    assert any("/srv/porter/.switchyard/provision'" in command for command in elsewhere), elsewhere


def test_a_refused_path_is_reported_rather_than_rendered() -> None:
    """The artifact runs as root, so a refusal must not arrive as a traceback."""
    import types

    from scripts import team_launcher

    config = types.SimpleNamespace(
        project="porter",
        run_as_user="foo",
        worktree_base=Path("/home/foo/porter-worktrees"),
        roles=[types.SimpleNamespace(role="main", workdir="/home/foo/porter-worktrees/main")],
        # The sibling home, not this owner's.
        control_repository=Path("/home/foobar/.local/state/switchyard/projects/porter/control.git"),
    )
    original = team_launcher.home_dir_for_user
    errors = io.StringIO()
    try:
        team_launcher.home_dir_for_user = lambda user: Path("/home/foo")
        with contextlib.redirect_stderr(errors):
            commands = team_launcher.role_path_access_commands(config)
    finally:
        team_launcher.home_dir_for_user = original
    assert commands == [], commands
    assert "no role path access granted for porter" in errors.getvalue(), errors.getvalue()
    assert "/home/foobar" in errors.getvalue(), errors.getvalue()


def main() -> int:
    test_containment_is_by_component_not_by_prefix()
    test_a_refused_path_is_reported_rather_than_rendered()
    owner = pwd.getpwuid(os.geteuid()).pw_name
    command = [sys.executable, str(Path(__file__).resolve()), "--namespace-child", owner]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    subprocess.run(command, check=True)
    print("team_launcher_role_path_access_test: ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--namespace-child":
        role_and_director_access(sys.argv[2])
    else:
        raise SystemExit(main())
