#!/usr/bin/env python3
"""SYRD-87 R6: a deploy must probe the board it deployed, not the default one.

The R5 rollout started the target board correctly and then rolled it back. The
rendered deploy command did not carry BOARD_PORT, so ticket-board-service.sh
used its own fallback of 8770 -- which on this multi-tenant host is another
tenant's board, and is listening. The HTTP smoke therefore passed against
http://127.0.0.1:8770/api/board, and the build-id gate spent its whole ten
second budget comparing that foreign board's build to this release's before
rolling back a process that had started correctly.

A health gate that can pass by reaching somebody else's service is not a check
on this one. These pin the probe target to the tenant's own plan.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher  # noqa: E402

#: The fallback in scripts/ticket-board-service.sh. Named here so the coupling
#: is visible: if that default changes, this test should be the thing that says
#: so rather than a rollout.
SERVICE_DEFAULT_PORT = "8770"


def fail(message: str) -> None:
    raise AssertionError(message)


def _service_default_port() -> str:
    body = (ROOT / "scripts" / "ticket-board-service.sh").read_text(encoding="utf-8")
    for line in body.splitlines():
        if line.startswith("readonly BOARD_PORT="):
            return line.split("BOARD_PORT:-", 1)[1].split("}", 1)[0]
    fail("could not read the deploy script's BOARD_PORT default")
    return ""


def _status(**overrides):
    base = dict(
        board_root=Path("/home/owner/porter-ticketboard-live"),
        owner_user="owner",
        owner_home=Path("/home/owner"),
        provisioned_system_unit=Path("/etc/switchyard/provision/porter/porter-ticket-board.service"),
        commit_git_dir="/home/owner/cache.git",
        current_release=Path("/home/owner/porter-ticketboard-live/releases/aaa"),
        current_sha="a" * 40,
        target_sha="b" * 40,
        deploy_ref="b" * 40,
        source_repo=Path("/opt/switchyard/releases/" + "b" * 40),
        board_port="23326",
        board_socket="/run/porter-ticket-board/ticket-board.sock",
    )
    base.update(overrides)
    return team_launcher.TenantReleaseStatus(**base)


def test_the_deploy_command_names_the_tenants_own_port() -> None:
    command = team_launcher.tenant_release_deploy_command(_status(), "porter")
    if "BOARD_PORT=23326" not in command:
        fail(f"the deploy command must name the tenant's port; got: {command}")
    default = _service_default_port()
    if default != SERVICE_DEFAULT_PORT:
        fail(
            f"the deploy script's fallback port is now {default}; this test pins "
            f"{SERVICE_DEFAULT_PORT} because that is the value that reached another tenant"
        )
    # The whole point: the command must not leave the script on its fallback.
    if f"BOARD_PORT={default}" in command:
        fail("the deploy command must not hand the script its own fallback port")


def test_the_deploy_command_names_the_tenants_own_socket() -> None:
    command = team_launcher.tenant_release_deploy_command(_status(), "porter")
    if "BOARD_UNIX_SOCKET=/run/porter-ticket-board/ticket-board.sock" not in command:
        fail(f"the deploy command must name the tenant's socket; got: {command}")


def test_a_plan_without_a_port_does_not_invent_one() -> None:
    """Absent is absent. Naming a guess would be the same defect again."""
    command = team_launcher.tenant_release_deploy_command(
        _status(board_port="", board_socket=""), "porter"
    )
    if "BOARD_PORT=" in command:
        fail("a tenant whose plan records no port must not have one invented for it")
    if "BOARD_UNIX_SOCKET=" in command:
        fail("a tenant whose plan records no socket must not have one invented for it")


def test_the_status_takes_the_port_from_the_tenant_plan() -> None:
    """End to end from a plan on disk, which is where the value went missing."""
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-port.") as tmp:
        root = Path(tmp)
        provision = root / "provision"
        provision.mkdir()
        board_root = root / "porter-ticketboard-live"
        (board_root / "releases").mkdir(parents=True)
        (provision / "plan.json").write_text(
            json.dumps(
                {
                    "project": "porter",
                    "owner_user": "owner",
                    "owner_home": str(root),
                    "board_root": str(board_root),
                    "port": 24999,
                    "socket_path": "/run/porter-ticket-board/ticket-board.sock",
                }
            ),
            encoding="utf-8",
        )
        config_path = provision / "porter.json"
        layout = provision / "layout.json"
        layout.write_text(json.dumps(team_launcher._new_project_layout_payload(1)), encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "session_dir": str(root / "sessions"),
                    "run_as_user": "owner",
                    "roles": [
                        {
                            "role": "ops",
                            "slot": 0,
                            "cli": ["claude"],
                            "live_commands": ["claude"],
                            "target": "porter-ops:0.0",
                            "tmux_session": "porter-ops",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        config = team_launcher.load_project_config("porter", config_path)
        status = team_launcher.tenant_release_status(
            config, config_path=config_path, deploy_ref="b" * 40
        )
        if status is None:
            fail("the fixture tenant should produce a release status")
        if status.board_port != "24999":
            fail(f"the status must carry the plan's port, got {status.board_port!r}")
        command = team_launcher.tenant_release_deploy_command(status, "porter")
        if "BOARD_PORT=24999" not in command:
            fail(f"the deploy command must carry the plan's port; got: {command}")


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    for name, fn in sorted(vars(module).items()):
        if name.startswith("test_") and callable(fn) and inspect.getmodule(fn) is module:
            fn()
    print("syrd_87_deploy_probe_target_test: ok")
