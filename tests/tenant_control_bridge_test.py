#!/usr/bin/env python3
"""The lifecycle bridge lets one recorded human start a tenant, and nothing else.

A tenant's owner account exists to keep the project's files, sessions and board
credentials away from everyone else, which leaves the human who provisioned it
unable to start their own project. The alternatives are worse than the problem:
a stored password is a reusable credential, and a broad sudo grant hands over
every command the owner could run. These pin the narrow route instead.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from standalone_test_runner import run_module_tests  # noqa: E402
from scripts import team_launcher  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    build_plan,
    invoking_human,
    privileged_artifact_names,
    render_operator_commands,
    resolve_control_user,
    tenant_control_commands,
    tenant_control_grant,
    TENANT_CONTROL_LAUNCHER,
    tenant_control_grant_name,
    tenant_control_helper_path,
    tenant_control_sudoers,
)

BRIDGE = ROOT / "scripts" / "switchyard-tenant-control"
PROJECT = "demo"
OWNER = "demo-agent"
HUMAN = "alice"


def _plan(**overrides: Any):
    fields = {
        "project": PROJECT,
        "owner_user": OWNER,
        "control_user": HUMAN,
        "source_repo": Path("/srv/switchyard"),
    }
    fields.update(overrides)
    return build_plan(**fields)


def _run_bridge(*args: str, environ: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BRIDGE), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={"PATH": "/usr/bin:/bin", **(environ or {})},
    )


def test_the_grant_records_only_a_username_and_no_credential() -> None:
    grant = json.loads(tenant_control_grant(_plan()))
    assert grant == {
        "project": PROJECT,
        "owner": OWNER,
        "authorized_user": HUMAN,
        "launcher": TENANT_CONTROL_LAUNCHER,
    }
    # The shared root-controlled release, NOT the tenant's own deployed tree.
    # That tree lives under the owner's home, where the owner can replace the
    # `current` symlink or any directory above the launcher -- a program the
    # tenant owner can swap is not a pinned entrypoint, whatever the grant says
    # and however root-owned its last component is (SYRD-50 review).
    assert grant["launcher"] == "/opt/switchyard/current/switchyard"
    assert OWNER not in grant["launcher"]
    assert "ticketboard-live" not in grant["launcher"]
    # Nothing here is reusable as a credential, which is the point: the whole
    # record is who may ask, and what they may ask for.
    serialized = json.dumps(grant).lower()
    for forbidden in ("password", "passwd", "secret", "token", "key", "hash"):
        assert forbidden not in serialized, forbidden


def test_the_sudo_grant_names_one_program_and_one_caller() -> None:
    rendered = tenant_control_sudoers(_plan())
    body = [line for line in rendered.splitlines() if line and not line.startswith("#")]
    assert body == [f"{HUMAN} ALL=(root) NOPASSWD: {tenant_control_helper_path(PROJECT)}"]
    # No shell, no editor, no wildcard, and no grant on the owner account
    # itself -- any of which would be a general foothold rather than a verb.
    for forbidden in ("ALL:", "*", "/bin/sh", "/bin/bash", "sudoedit", f"({OWNER})"):
        assert forbidden not in rendered, forbidden


def test_a_tenant_with_no_recorded_human_installs_no_bridge() -> None:
    plan = _plan(control_user="")
    assert tenant_control_sudoers(plan) == ""
    assert tenant_control_grant(plan) == ""


def test_the_bridge_refuses_everything_but_a_slug_and_a_known_verb() -> None:
    # No executable, no path, no shell fragment, no role, no extra argument.
    for args, expected in (
        ((), "usage"),
        ((PROJECT,), "usage"),
        ((PROJECT, "start", "extra"), "usage"),
        (("../../etc", "start"), "plain slug"),
        (("demo/../other", "start"), "plain slug"),
        (("demo;reboot", "start"), "plain slug"),
        ((PROJECT, "start; rm -rf /"), "operation must be one of"),
        ((PROJECT, "upgrade"), "operation must be one of"),
        ((PROJECT, "--config=/tmp/evil.json"), "operation must be one of"),
        ((PROJECT, "pane"), "operation must be one of"),
    ):
        result = _run_bridge(*args)
        assert result.returncode != 0, args
        assert expected in result.stdout, (args, result.stdout)


def test_a_rejected_argument_is_rejected_before_anything_is_read() -> None:
    """The allow-lists decide, not the filesystem.

    If a bad project or verb were only caught later, the decision would depend
    on which files happen to exist. These are refused with no grant installed
    at all, so nothing about the refusal is environmental.
    """
    source = BRIDGE.read_text(encoding="utf-8")
    assert source.index("fail(\"project must be a plain slug\")") < source.index("grant = load_grant(")
    assert source.index("operation must be one of") < source.index("grant = load_grant(")

    for args in (("../../etc", "start"), (PROJECT, "upgrade")):
        result = _run_bridge(*args)
        assert result.returncode != 0, args
        # Not "no control grant is installed": it never got that far.
        assert "control grant" not in result.stdout, (args, result.stdout)


def test_the_bridge_reads_the_caller_from_the_kernel_not_the_environment() -> None:
    """SUDO_USER is a string the caller can set; SUDO_UID is sudo's own answer.

    Trusting the name would let anyone who can reach the helper claim to be the
    authorized human, which is the entire access decision.
    """
    source = BRIDGE.read_text(encoding="utf-8")
    assert 'os.environ.get("SUDO_UID")' in source
    assert 'os.environ.get("SUDO_USER")' not in source
    assert 'environ["SUDO_USER"]' not in source


def test_an_unauthorized_local_user_is_refused_without_a_prompt() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-tenant-control.") as tmp:
        root = Path(tmp)
        (root / PROJECT).mkdir()
        (root / PROJECT / "control-grant.json").write_text(
            json.dumps({"project": PROJECT, "owner": OWNER, "authorized_user": HUMAN, "launcher": "/x"}),
            encoding="utf-8",
        )
        grant = team_launcher._tenant_control_grant(PROJECT, root=root)
        assert grant["authorized_user"] == HUMAN

        calls: list[list[str]] = []

        def exec_func(program: str, argv: list[str]) -> None:
            calls.append([program, *argv])

        original = team_launcher.current_user_name
        team_launcher.current_user_name = lambda: "mallory"  # type: ignore[assignment]
        try:
            team_launcher._switchyard_exec_through_tenant_control(
                PROJECT, "start", grant=grant, exec_func=exec_func
            )
            raise AssertionError("an unauthorized user was allowed through")
        except SystemExit as exit_error:
            message = str(exit_error)
            assert "mallory may not control demo" in message
            assert HUMAN in message
        finally:
            team_launcher.current_user_name = original  # type: ignore[assignment]

        # Refused locally: sudo is never reached, so there is no prompt to see.
        assert calls == []


def test_the_authorized_human_reaches_the_owner_through_the_bridge_alone() -> None:
    grant = {"project": PROJECT, "owner": OWNER, "authorized_user": HUMAN, "launcher": "/x"}
    calls: list[list[str]] = []

    def exec_func(program: str, argv: list[str]) -> None:
        calls.append([program, *argv])

    original = team_launcher.current_user_name
    team_launcher.current_user_name = lambda: HUMAN  # type: ignore[assignment]
    try:
        team_launcher._switchyard_exec_through_tenant_control(
            PROJECT, "start", grant=grant, exec_func=exec_func
        )
        raise AssertionError("the bridge should have exec'd")
    except SystemExit:
        pass
    finally:
        team_launcher.current_user_name = original  # type: ignore[assignment]

    assert len(calls) == 1
    argv = calls[0][1:]
    # -n: never prompt. And only the project and the verb cross the boundary --
    # no config, no source path, no role, no environment.
    assert argv == ["sudo", "-n", tenant_control_helper_path(PROJECT), PROJECT, "start"]


def test_the_wrapper_uses_the_bridge_first_and_sudo_only_otherwise() -> None:
    """A start goes over the bridge; anything else keeps the operator path.

    Routing everything through the bridge would mean widening it, and falling
    back to sudo for a start would put a password prompt in front of the human
    the tenant was provisioned for.
    """
    with tempfile.TemporaryDirectory(prefix="switchyard-tenant-control.") as tmp:
        root = Path(tmp)
        (root / PROJECT).mkdir()
        (root / PROJECT / "control-grant.json").write_text(
            json.dumps({"project": PROJECT, "owner": OWNER, "authorized_user": HUMAN, "launcher": "/x"}),
            encoding="utf-8",
        )

        taken: list[str] = []
        original_grant = team_launcher._tenant_control_grant
        original_bridge = team_launcher._switchyard_exec_through_tenant_control
        original_root = team_launcher._switchyard_exec_with_root
        team_launcher._tenant_control_grant = lambda project, root=root: original_grant(  # type: ignore[assignment]
            project, root=root
        )
        team_launcher._switchyard_exec_through_tenant_control = (  # type: ignore[assignment]
            lambda project, operation, **kwargs: taken.append(f"bridge:{operation}")
        )
        team_launcher._switchyard_exec_with_root = lambda argv, **kwargs: taken.append(  # type: ignore[assignment]
            f"sudo:{' '.join(argv)}"
        )
        try:
            team_launcher._switchyard_cross_account(PROJECT, [PROJECT])
            team_launcher._switchyard_cross_account(PROJECT, ["stop", PROJECT])
            team_launcher._switchyard_cross_account(PROJECT, ["upgrade", PROJECT])
        finally:
            team_launcher._tenant_control_grant = original_grant  # type: ignore[assignment]
            team_launcher._switchyard_exec_through_tenant_control = original_bridge  # type: ignore[assignment]
            team_launcher._switchyard_exec_with_root = original_root  # type: ignore[assignment]

    # The two lifecycle calls fall through to sudo as well only because the
    # stubbed bridge returns; a real one execs. What matters is which came first.
    assert taken[0] == "bridge:start"
    assert "bridge:stop" in taken
    assert taken[-1] == "sudo:upgrade demo"
    assert "bridge:upgrade" not in taken


def test_a_tenant_with_no_bridge_still_reaches_the_owner_through_sudo() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-tenant-control.") as tmp:
        root = Path(tmp)
        assert team_launcher._tenant_control_grant(PROJECT, root=root) == {}

        # A grant naming another project is not this project's grant.
        (root / PROJECT).mkdir()
        (root / PROJECT / "control-grant.json").write_text(
            json.dumps({"project": "other", "authorized_user": HUMAN}), encoding="utf-8"
        )
        assert team_launcher._tenant_control_grant(PROJECT, root=root) == {}


def test_only_lifecycle_verbs_take_the_bridge() -> None:
    for argv, expected in (
        ([PROJECT], "start"),
        (["stop", PROJECT], "stop"),
        (["status", PROJECT], "status"),
        # Everything else keeps the operator path.
        (["upgrade", PROJECT], ""),
        (["teardown", PROJECT], ""),
        (["new"], ""),
        ([PROJECT, "--layout", "viewer"], ""),
        (["stop", "other-project"], ""),
    ):
        assert team_launcher._tenant_control_operation(argv, PROJECT) == expected, argv


def test_repair_reinstalls_the_bridge_idempotently() -> None:
    plan = _plan()
    commands = tenant_control_commands(PROJECT, OWNER, HUMAN)
    joined = "\n".join(commands)
    # visudo before the file is moved into place, so a bad render cannot leave
    # sudoers.d unparsable and lock everyone out of sudo.
    assert joined.index("visudo -c -f") < joined.index("sudo mv ")
    assert "install -m 0440 -o root -g root" in joined
    assert "install -m 0644 -o root -g root" in joined
    # The bytes are written here, not fetched at run time, so a repair and a
    # fresh provision of the same tenant install exactly the same files.
    assert tenant_control_grant(plan).strip() in joined
    assert tenant_control_sudoers(plan).strip() in joined
    # Re-running rewrites; it never appends, and never creates a second rule.
    assert tenant_control_commands(PROJECT, OWNER, HUMAN) == commands
    sudoers = f"/etc/sudoers.d/{plan.tenant_control_sudoers_name}"
    assert len([line for line in commands if line.startswith("sudo mv ")]) == 1
    assert joined.count(f"{sudoers}'") == 1, "the live rule is written exactly once"
    assert ">>" not in joined, "nothing is appended, so a rerun cannot accumulate"
    # A tenant with no recorded human installs nothing at all.
    assert tenant_control_commands(PROJECT, OWNER, "") == []


def test_a_grant_the_tenant_could_have_written_is_not_an_authority() -> None:
    """A root-owned grant decides who may control the tenant; nothing else does.

    The case where an installed root-owned grant wins over the operator running
    a repair needs a genuinely root-owned file, so it is proved in
    tenant_control_bridge_e2e_test.py.
    """
    with tempfile.TemporaryDirectory(prefix="switchyard-control-resolve.") as tmp:
        root = Path(tmp)
        (root / PROJECT).mkdir()
        grant = root / PROJECT / "control-grant.json"
        grant.write_text(
            json.dumps({"project": PROJECT, "owner": OWNER, "authorized_user": "mallory"}),
            encoding="utf-8",
        )
        # This file is owned by whoever runs the suite, not by root, so it does
        # not get to name the controller; the invoking human is used instead.
        assert os.stat(grant).st_uid != 0
        assert resolve_control_user(PROJECT, invoking_user="bob", owner_user=OWNER, root=root) == "bob"

        # A grant naming a different project is not this project's authority.
        grant.write_text(
            json.dumps({"project": "other", "owner": OWNER, "authorized_user": "mallory"}),
            encoding="utf-8",
        )
        assert resolve_control_user(PROJECT, invoking_user="bob", owner_user=OWNER, root=root) == "bob"


def test_the_tenants_own_accounts_are_never_recorded_as_the_controller() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-control-resolve.") as tmp:
        root = Path(tmp)
        # The owner already is the owner; a bridge for it would say nothing.
        assert resolve_control_user(PROJECT, invoking_user=OWNER, owner_user=OWNER, root=root) == ""
        # A role account must not gain a route into the owner: the distinct role
        # UIDs and the board's peer-credential checks are what that separation is.
        assert resolve_control_user(PROJECT, invoking_user=f"{PROJECT}-app", owner_user=OWNER, root=root) == ""
        assert resolve_control_user(PROJECT, invoking_user="root", owner_user=OWNER, root=root) == ""
        assert resolve_control_user(PROJECT, invoking_user="", owner_user=OWNER, root=root) == ""


def test_the_provisioning_human_is_read_from_the_kernels_answer() -> None:
    me = pwd.getpwuid(os.getuid()).pw_name
    # No sudo: the person running it.
    assert invoking_human({}) == me
    # Through sudo: the uid sudo resolved, not the name the caller passed.
    assert invoking_human({"SUDO_UID": str(os.getuid()), "SUDO_USER": "root"}) == me
    assert invoking_human({"SUDO_USER": me}) == me
    # Root running it directly records nobody, so a root cron job cannot mint a
    # grant for an account nobody asked for.
    assert invoking_human({"SUDO_UID": "0", "SUDO_USER": "root"}) in {me, ""}


def test_fresh_provisioning_installs_the_bridge_as_a_privileged_artifact() -> None:
    """Root installs the grant and the rule, from the root-owned mirror.

    Both decide who may cross into the owner account, so a tenant that could
    edit either would choose its own controller. That is the same reason the
    role control sudoers file is a privileged artifact.
    """
    plan = _plan()
    names = privileged_artifact_names(plan)
    assert plan.tenant_control_sudoers_name in names
    assert tenant_control_grant_name(PROJECT) in names

    commands = render_operator_commands(plan).splitlines()

    def line_index(predicate) -> int:
        return next(index for index, line in enumerate(commands) if predicate(line))

    staged = line_index(lambda line: line.endswith(f"'{tenant_control_helper_path(PROJECT)}'"))
    grant = line_index(lambda line: line.endswith("'/usr/local/lib/switchyard/demo/control-grant.json'"))
    check = line_index(lambda line: line.startswith("sudo visudo") and "tenant-control" in line)
    live = line_index(lambda line: line.startswith("sudo mv") and "tenant-control" in line)
    # The rule names the helper, so the helper exists first; the grant that
    # constrains the helper lands before the rule goes live; and visudo passes
    # before anything reaches sudoers.d.
    assert staged < grant < check < live, (staged, grant, check, live)


def test_a_tenant_with_no_recorded_human_provisions_no_bridge() -> None:
    plan = _plan(control_user="")
    names = privileged_artifact_names(plan)
    assert plan.tenant_control_sudoers_name not in names
    assert tenant_control_grant_name(PROJECT) not in names
    rendered = render_operator_commands(plan)
    assert "/etc/sudoers.d/49-demo-tenant-control" not in rendered
    assert "control-grant.json" not in rendered
    assert "no lifecycle control bridge" in rendered
    # The helper itself is still staged, root-owned and inert: with no grant it
    # refuses every caller, and a later repair that records a human then has
    # nothing left to install but the grant and the rule.
    assert f"'{tenant_control_helper_path(PROJECT)}'" in rendered


def test_teardown_removes_the_grant_and_the_sudo_rule() -> None:
    plan = _plan()
    actions = team_launcher._switchyard_teardown_actions(
        plan,
        registry_path=Path("/tmp/registry"),
        home_base=Path("/home"),
        remove_owner_home=False,
        remove_owner_user=False,
    )
    removed = {" ".join(action.command) for action in actions}
    assert f"rm -f /etc/sudoers.d/{plan.tenant_control_sudoers_name}" in removed
    assert f"rm -f /usr/local/lib/switchyard/{PROJECT}/control-grant.json" in removed
    assert f"rm -f {tenant_control_helper_path(PROJECT)}" in removed


if __name__ == "__main__":
    run_module_tests(globals())
    print("tenant_control_bridge_test: ok")
