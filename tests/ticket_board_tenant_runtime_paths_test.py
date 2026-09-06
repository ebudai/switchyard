#!/usr/bin/env python3
"""Regression tests for tenant-owned paths and immutable release tooling."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from scripts import team_launcher
from scripts.ticket_board.notify_listener import DirectorctlSender
from scripts.ticket_board.project_provision import build_plan, render_board_unit, render_listener_unit, render_operator_commands, write_artifacts
from scripts.ticket_board.runtime_paths import directorctl_path


def test_release_local_directorctl_never_selects_another_home() -> None:
    module = Path("/srv/switchyard/releases/abc/scripts/ticket_board/runtime_paths.py")
    with patch.dict(os.environ, {}, clear=True):
        assert directorctl_path(module) == "/srv/switchyard/releases/abc/scripts/directorctl"
    with patch.dict(os.environ, {"TICKET_BOARD_DIRECTORCTL": "/opt/reviewed/scripts/directorctl"}, clear=True):
        assert directorctl_path(module) == "/opt/reviewed/scripts/directorctl"
    with patch.dict(os.environ, {"TICKET_BOARD_DIRECTORCTL": "relative/directorctl"}, clear=True):
        try:
            directorctl_path(module)
        except RuntimeError as exc:
            assert "must be an absolute" in str(exc)
        else:
            raise AssertionError("relative directorctl override should fail closed")


def test_custom_owner_home_provisions_every_runtime_path() -> None:
    owner_home = Path("/srv/tenants/orbit-owner")
    plan = build_plan(
        project="orbit",
        owner_user="orbit-worker",
        owner_home=owner_home,
        source_repo=ROOT,
        commit_git_dir=owner_home / ".local/state/switchyard/projects/orbit/control.git",
    )
    rendered = "\n".join((render_board_unit(plan), render_listener_unit(plan), render_operator_commands(plan)))
    assert plan.owner_home == str(owner_home)
    assert plan.board_root == str(owner_home / "orbit-ticketboard-live")
    assert f"TICKET_BOARD_DIRECTORCTL={plan.board_current}/scripts/directorctl" in rendered
    assert f"--directorctl {plan.board_current}/scripts/directorctl" in rendered
    assert f"HOME='{owner_home}'" in rendered
    assert f"TICKET_BOARD_OWNER_HOME='{owner_home}'" in rendered
    assert 'TICKET_BOARD_PROVISIONED_SYSTEM_UNIT="$system_unit_candidate"' in rendered
    assert "/home/agent" not in rendered
    assert "/home/orbit-worker" not in rendered


def test_relative_tenant_storage_paths_are_rejected() -> None:
    for field, value in (
        ("board_root", Path("other/tenant-live")),
        ("asset_dir", Path("other/assets")),
        ("frame_dir", Path("other/frames")),
    ):
        try:
            build_plan(
                project="orbit",
                owner_user="orbit-worker",
                owner_home=Path("/srv/tenants/orbit-owner"),
                **{field: value},
            )
        except SystemExit as exc:
            assert "absolute tenant path" in str(exc)
        else:
            raise AssertionError(f"relative {field} should fail closed")


def test_exported_artifacts_persist_owner_home_and_generated_unit_provenance() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-exported-tenant.") as tmp:
        root = Path(tmp)
        release = root / "release"
        (release / "scripts").mkdir(parents=True)
        (release / "scripts" / "ticket-board-service.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        owner_home = root / "homes" / "nova"
        plan = build_plan(project="nova", owner_user="nova-user", owner_home=owner_home, source_repo=release)
        output = root / "project" / ".switchyard" / "provision"
        write_artifacts(plan, output)
        persisted = json.loads((output / "plan.json").read_text(encoding="utf-8"))
        commands = (output / "operator-commands.sh").read_text(encoding="utf-8")
        unit = output / "nova-ticket-board.service"
        canary_unit = output / "nova-ticket-board-canary.service"

        assert persisted["owner_home"] == str(owner_home)
        assert unit.is_file()
        assert canary_unit.is_file()
        assert f'system_unit_candidate="$provision_dir/{unit.name}"' in commands
        assert f'canary_unit_candidate="$provision_dir/{canary_unit.name}"' in commands
        assert f'sudo install -m 0644 "$canary_unit_candidate" \'/etc/systemd/system/{canary_unit.name}\'' in commands
        assert f"sudo -u 'nova-user' -H env HOME='{owner_home}'" in commands
        assert f"SOURCE_REPO='{release}'" in commands
        assert "deploy/systemd/nova-ticket-board.service.boardsvc" not in commands


def test_generated_role_path_prefers_its_selected_release_directorctl() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-role-path.") as tmp:
        root = Path(tmp)
        plan = build_plan(
            project="orbit",
            owner_user="orbit-worker",
            owner_home=Path("/srv/tenants/orbit-owner"),
            source_repo=ROOT,
        )
        payload = team_launcher._new_project_launcher_config_payload(plan, repository=Path("/srv/projects/orbit"))
        role = payload["roles"][0]
        assert role["env"]["TICKET_BOARD_DIRECTORCTL"] == f"{plan.board_current}/scripts/directorctl"
        config_path = root / "orbit.json"
        (root / payload["layout"]).write_text(
            json.dumps(team_launcher._new_project_layout_payload(6)), encoding="utf-8"
        )
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        config_role = team_launcher.load_project_config("orbit", config_path).roles[0]
        command = team_launcher.cli_command_for_role(
            config_role,
            session_dir=Path("/srv/tenants/orbit-owner/.local/state/orbit-ticket-board/pane-sessions"),
            bin_user="orbit-worker",
        )
        path_arg = next(item for item in command if item.startswith("PATH="))
        assert path_arg.split("=", 1)[1].split(":", 1)[0] == f"{plan.board_current}/scripts"


def test_release_sender_executes_only_selected_directorctl() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-release-send.") as tmp:
        root = Path(tmp)
        release = root / "releases" / "reviewed"
        scripts = release / "scripts"
        scripts.mkdir(parents=True)
        capture = root / "capture.json"
        tool = scripts / "directorctl"
        tool.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "open(os.environ['CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        tool.chmod(0o755)
        sender = DirectorctlSender(str(tool))
        with patch.dict(os.environ, {"CAPTURE": str(capture)}, clear=False):
            result = sender("orbit-main:0.0", "New ticket for you: ORB-1 -- Delivery")
        assert result == {}
        assert json.loads(capture.read_text(encoding="utf-8")) == [
            "send",
            "orbit-main:0.0",
            "New ticket for you: ORB-1 -- Delivery",
        ]


def test_service_helpers_fail_closed_without_tenant_root() -> None:
    clean_env = {
        "HOME": "/tmp/orbit-owner",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TICKET_BOARD_PROJECT": "orbit",
    }
    for script in ("ticket-board-service.sh", "ticket-board-notify-listener-service.sh"):
        proc = subprocess.run(
            [str(ROOT / "scripts" / script), "status"],
            env=clean_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.returncode != 0
        assert "BOARD_ROOT is required" in proc.stderr
        assert "refusing to select another tenant's home" in proc.stderr
        assert "/home/agent" not in proc.stderr


def test_copy_installer_requires_explicit_absolute_destination() -> None:
    env = {"HOME": "/tmp/orbit-owner", "PATH": "/usr/local/bin:/usr/bin:/bin"}
    proc = subprocess.run(
        [str(ROOT / "scripts" / "install-directorctl")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.returncode != 0
    assert "DIRECTORCTL_INSTALL_PATH is required" in proc.stderr
    assert "/home/agent" not in proc.stderr


def main() -> int:
    run_module_tests(globals())
    print("ticket_board_tenant_runtime_paths_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
