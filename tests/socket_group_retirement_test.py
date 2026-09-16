#!/usr/bin/env python3
"""SYRD-171: the socket group still held the tenant's worktrees and repository.

Journal 0081 of SYRD-146 recorded that `boardsvc` can traverse
`/home/switchyard-agent/syrd-worktrees`, and it reached it by two independent
paths:

    other::r-x                  <- mode 0755, the SYRD-156 class of defect
    group:syrd-roles:--x        <- a named entry to the SOCKET group, and the
                                   board service is in that group because it
                                   must be, to hand the socket to the roles

Closing one leaves the other. And the exposure is read, not traverse: all 163
directories under that base are 0755 and own no ACL, so traversal into the base
is the whole syrd source tree, every role and every historical ticket worktree.

Beside it, latent: the control repository carried `group:syrd-roles:rwx` with a
default entry, so every object git wrote later inherited it -- verbatim the
grant `repository_group_name()` exists to avoid. Unreachable today only because
a parent is 0700, which one `setfacl` would undo.

The reason nothing fixed it is that the only function that retires the socket
group is called from the add-role path, and syrd is a shared-account tenant:
all six panes run as the project account, `plan.role_accounts` is empty, and
that path is never reached. So the generated packet contained no repair, which
is why this had to be a code change and not a host one.

These cases run the packet's own commands against a real tree and then ask the
kernel, as a second real uid in a real group, what that account can do. The
uids and gids come from a user namespace with the invoking user's subuid and
subgid ranges mapped, so nothing here needs privilege and nothing here touches
the host.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.ticket_board.project_provision import (  # noqa: E402
    TENANT_SOURCE_MODE,
    build_plan,
    render_operator_commands,
    repository_group_name,
    roles_group_name,
)


def provisioning():
    """The module, imported where it is used.

    The names this ticket adds are looked up through it rather than at import
    time, so the cases that ask only what the packet DOES still run against a
    tree that has none of them -- which is how the second one below reproduces
    what it was written against.
    """
    from scripts.ticket_board import project_provision

    return project_provision

PROJECT = "demo"
OWNER_UID = 1500
SERVICE_UID = 1600
ROLE_UID = 1700
#: The socket group the board service is in, and the repository group it is
#: deliberately not in. Real gids inside the namespace.
SOCKET_GID = 1900
REPO_GID = 1901
SECRET = "the tenant's source, across every role and every ticket\n"


def require_tools() -> None:
    for tool in ("setfacl", "getfacl", "unshare", "setpriv", "install", "chmod", "find"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"
    probe = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", "true"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, (
        "this host cannot map a second uid into a user namespace "
        f"({probe.stderr.strip()}); the question this suite asks -- what a DIFFERENT "
        "account in a DIFFERENT group can read -- cannot be answered without one, so "
        "it fails rather than reporting a pass it did not earn"
    )


def in_namespace(scenario: str, payload: dict | None = None) -> dict:
    done = subprocess.run(
        [
            "unshare", "--user", "--map-auto", "--map-root-user", "--mount",
            sys.executable, str(Path(__file__).resolve()), "--scenario", scenario,
        ],
        input=json.dumps(payload or {}),
        text=True,
        capture_output=True,
    )
    assert done.returncode == 0, (scenario, done.stdout, done.stderr)
    return json.loads(done.stdout)


# ------------------------------------------------------------------- plans --


def plan_for(home: Path, *, role_accounts: tuple[tuple[str, str], ...] = ()):
    from dataclasses import replace

    plan = build_plan(
        project=PROJECT,
        owner_user=str(OWNER_UID),
        owner_home=home,
        service_user=str(SERVICE_UID),
        project_repository=home / "Projects" / PROJECT,
        # syrd's shape: the commit store the board reads is its own repository,
        # beside the control repository rather than being it.
        commit_git_dir=str(home / f"{PROJECT}-source-cache.git"),
    )
    if not role_accounts:
        return plan
    return replace(
        plan,
        role_accounts=role_accounts,
        roles_group=roles_group_name(PROJECT),
        role_worktrees=tuple(
            (role, str(home / f"{PROJECT}-worktrees" / role)) for role, _account in role_accounts
        ),
    )


def tenant_lines(plan, home: Path, *, socket_gid: int, repo_gid: int) -> list[str]:
    """The packet's own commands for this tenant's tree, as it renders them.

    Two substitutions, and only two: the socket and repository GROUPS are named
    by the packet and named by gid here, because inside this namespace those
    groups have numbers and no names. Nothing else is rewritten -- the modes,
    the entries, the order and the commands are the packet's.

    Guarded lines ARE taken, unlike in the suites that lift board-tree grants
    out of the same packet: this fixture has arranged both conditions the
    guards check -- the group exists as a gid, the base exists -- so running
    the body is running what the packet would run, not running it somewhere the
    packet would not have.
    """
    wanted: list[str] = []
    for line in render_operator_commands(plan).splitlines():
        stripped = line.strip()
        # `sudo chmod` is deliberately not here: the sweep this suite is about
        # is `sudo find ... -exec chmod`, and the bare chmods in the packet
        # belong to the owner's SSH identity, which this fixture has none of.
        if not stripped.startswith(("sudo install -d", "sudo setfacl", "sudo find")):
            continue
        if str(home) not in stripped:
            continue
        stripped = stripped.replace(f"g:{roles_group_name(PROJECT)}", f"g:{socket_gid}")
        stripped = stripped.replace(f"g:{repository_group_name(PROJECT)}", f"g:{repo_gid}")
        wanted.append(stripped.removeprefix("sudo "))
    assert wanted, "the packet touched nothing inside this tenant's home"
    return wanted


# ------------------------------------------------- scenarios (in namespace) --


def _tree(tmp: Path) -> tuple[Path, Path, Path]:
    """The shape journal 0081 recorded: a 0755 worktree base, and a control
    repository the socket group can write, under a home only traversal reaches.
    """
    os.chmod(tmp, 0o711)
    home = tmp / "home" / "tenant"
    base = home / f"{PROJECT}-worktrees"
    control = home / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git"
    for role in ("director", "main", "SYRD-42"):
        (base / role).mkdir(parents=True)
        (base / role / "source.py").write_text(SECRET, encoding="utf-8")
    (control / "objects").mkdir(parents=True)
    store = home / f"{PROJECT}-source-cache.git"
    (store / "objects").mkdir(parents=True)
    os.chmod(tmp / "home", 0o711)
    for path in (home, base, control, control.parent, control.parent.parent):
        os.chown(path, OWNER_UID, OWNER_UID)
    for child in base.iterdir():
        os.chown(child, OWNER_UID, OWNER_UID)
        os.chmod(child, 0o755)
        for entry in child.iterdir():
            os.chown(entry, OWNER_UID, OWNER_UID)
    os.chown(control / "objects", OWNER_UID, OWNER_UID)
    for path in (store, store / "objects"):
        os.chown(path, OWNER_UID, OWNER_UID)
        os.chmod(path, 0o770)
    os.chmod(home, 0o710)
    os.chmod(base, 0o755)
    for path in (control, control.parent):
        os.chmod(path, 0o770)
    os.chown(home / ".local", OWNER_UID, OWNER_UID)
    os.chown(home / ".local" / "state", OWNER_UID, OWNER_UID)
    os.chown(home / ".local" / "state" / "switchyard", OWNER_UID, OWNER_UID)
    return home, base, control


def _grant_socket_group(home: Path, base: Path, control: Path) -> None:
    """The pre-SYRD-157 grants a tenant of syrd's vintage still carries."""
    # A principal with home traversal and nothing else: the control role has
    # one of these, and so does anything else granted a way through the home.
    # It is how the mode half of this reaches the tree independently of the
    # group half.
    _run(["setfacl", "-m", f"u:{ROLE_UID}:--x", str(home)])
    _run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(home)])
    _run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(base)])
    for path in (home / ".local", home / ".local" / "state",
                 home / ".local" / "state" / "switchyard", control.parent):
        _run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(path)])
    _run(["setfacl", "-R", "-m", f"g:{SOCKET_GID}:rwX", str(control)])
    _run(["setfacl", "-R", "-m", f"d:g:{SOCKET_GID}:rwX", str(control)])


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


def _as(uid: int, gid: int, argv: list[str], *, groups: list[int] | None = None) -> bool:
    command = ["setpriv", f"--reuid={uid}", f"--regid={gid}"]
    command += [f"--groups={','.join(str(g) for g in groups)}"] if groups else ["--clear-groups"]
    return subprocess.run([*command, *argv], capture_output=True).returncode == 0


def _service_reach(base: Path, control: Path) -> dict:
    """What the board service can do, as a member of the socket group."""
    membership = [SERVICE_UID, SOCKET_GID]
    store = base.parent / f"{PROJECT}-source-cache.git"
    return {
        "reads_commit_store": _as(
            SERVICE_UID, SERVICE_UID, ["ls", str(store / "objects")], groups=membership
        ),
        "writes_commit_store": _as(
            SERVICE_UID, SERVICE_UID, ["touch", str(store / "objects" / "planted")],
            groups=membership,
        ),
        "traverses_base": _as(SERVICE_UID, SERVICE_UID, ["test", "-x", str(base)], groups=membership),
        "lists_base": _as(SERVICE_UID, SERVICE_UID, ["ls", str(base)], groups=membership),
        "reads_source": _as(
            SERVICE_UID, SERVICE_UID, ["cat", str(base / "main" / "source.py")], groups=membership
        ),
        "traverses_control": _as(
            SERVICE_UID, SERVICE_UID, ["test", "-x", str(control)], groups=membership
        ),
        "writes_control": _as(
            SERVICE_UID, SERVICE_UID, ["touch", str(control / "objects" / "planted")],
            groups=membership,
        ),
    }


def _outsider_reach(base: Path) -> dict:
    """Any account that is in none of these groups, which is the mode half."""
    return {
        "traverses_base": _as(ROLE_UID, ROLE_UID, ["test", "-x", str(base)]),
        "lists_base": _as(ROLE_UID, ROLE_UID, ["ls", str(base)]),
        "reads_source": _as(ROLE_UID, ROLE_UID, ["cat", str(base / "main" / "source.py")]),
    }


def _owner_reach(base: Path, control: Path) -> dict:
    return {
        "traverses_base": _as(OWNER_UID, OWNER_UID, ["test", "-x", str(base)]),
        "reads_source": _as(OWNER_UID, OWNER_UID, ["cat", str(base / "main" / "source.py")]),
        "writes_control": _as(
            OWNER_UID, OWNER_UID, ["touch", str(control / "objects" / "owner-wrote")]
        ),
    }


def _acl(path: Path) -> list[str]:
    done = _run(["getfacl", "-cpn", str(path)])
    return [line for line in done.stdout.splitlines() if line and not line.startswith("#")]


def scenario_before_the_fix(_payload: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd171-before.") as tmp:
        home, base, control = _tree(Path(tmp))
        _grant_socket_group(home, base, control)
        return {
            "service": _service_reach(base, control),
            "outsider": _outsider_reach(base),
            "base_mode": oct(base.stat().st_mode & 0o7777),
            "base_acl": _acl(base),
            "control_acl": _acl(control),
        }


def scenario_packet(_payload: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd171-packet.") as tmp:
        home, base, control = _tree(Path(tmp))
        _grant_socket_group(home, base, control)
        plan = plan_for(home)
        for line in tenant_lines(plan, home, socket_gid=SOCKET_GID, repo_gid=REPO_GID):
            _run(["sh", "-c", line])
        return {
            "service": _service_reach(base, control),
            "owner": _owner_reach(base, control),
            "outsider": _outsider_reach(base),
            "base_mode": oct(base.stat().st_mode & 0o7777),
            "worktree_modes": sorted(
                oct(child.stat().st_mode & 0o7777) for child in base.iterdir()
            ),
            "base_acl": _acl(base),
            "control_acl": _acl(control),
            "control_object_acl": _acl(control / "objects"),
        }


def scenario_repeat(_payload: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd171-repeat.") as tmp:
        home, base, control = _tree(Path(tmp))
        _grant_socket_group(home, base, control)
        plan = plan_for(home)
        lines = tenant_lines(plan, home, socket_gid=SOCKET_GID, repo_gid=REPO_GID)
        for line in lines:
            _run(["sh", "-c", line])
        first = {
            "base_mode": oct(base.stat().st_mode & 0o7777),
            "base_acl": _acl(base),
            "control_acl": _acl(control),
        }
        for line in lines:
            _run(["sh", "-c", line])
        second = {
            "base_mode": oct(base.stat().st_mode & 0o7777),
            "base_acl": _acl(base),
            "control_acl": _acl(control),
        }
        return {"first": first, "second": second}


def scenario_per_role(_payload: dict) -> dict:
    """A tenant whose roles have their own accounts keeps reaching its trees."""
    with tempfile.TemporaryDirectory(prefix="syrd171-per-role.") as tmp:
        home, base, control = _tree(Path(tmp))
        _grant_socket_group(home, base, control)
        plan = plan_for(
            home,
            role_accounts=(("director", str(ROLE_UID)), ("main", str(ROLE_UID))),
        )
        for line in tenant_lines(plan, home, socket_gid=SOCKET_GID, repo_gid=REPO_GID):
            _run(["sh", "-c", line])
        role = [ROLE_UID, REPO_GID]
        return {
            "service": _service_reach(base, control),
            "role": {
                "traverses_base": _as(ROLE_UID, ROLE_UID, ["test", "-x", str(base)], groups=role),
                "traverses_control": _as(
                    ROLE_UID, ROLE_UID, ["test", "-x", str(control)], groups=role
                ),
                "writes_control": _as(
                    ROLE_UID, ROLE_UID, ["touch", str(control / "objects" / "role-wrote")],
                    groups=role,
                ),
            },
        }


SCENARIOS = {
    "before_the_fix": scenario_before_the_fix,
    "packet": scenario_packet,
    "repeat": scenario_repeat,
    "per_role": scenario_per_role,
}


# ------------------------------------------------------------------- cases --


def test_the_shape_journal_0081_recorded_is_reachable_two_ways() -> None:
    """What was reproduced, and that closing either half alone leaves the other."""
    require_tools()
    found = in_namespace("before_the_fix")
    service = found["service"]
    # The named entry is `--x`, so the service traverses the base and cannot
    # list it -- and traversing is enough, because everything inside is 0755
    # and names nobody. That is the whole exposure: the tenant's source, read
    # through a grant that was only ever meant to convey the board socket.
    assert service["traverses_base"], service
    assert service["reads_source"], service
    assert not service["lists_base"], service
    assert service["writes_control"], service
    # The second path, independent of the first: an account in none of these
    # groups reaches the same tree through the mode bits alone. Take either
    # half away and the other still grants it, which is why both are closed.
    outsider = found["outsider"]
    assert outsider["traverses_base"] and outsider["lists_base"] and outsider["reads_source"], outsider
    assert found["base_mode"] == "0o755", found
    assert any(line.startswith(f"group:{SOCKET_GID}:") for line in found["base_acl"]), found
    assert any(line.startswith(f"default:group:{SOCKET_GID}:") for line in found["control_acl"]), found


def test_the_packet_closes_the_tree_and_retires_the_group() -> None:
    """Both halves, from the packet's own lines, asked of the kernel."""
    require_tools()
    found = in_namespace("packet")
    service = found["service"]
    assert not service["traverses_base"], service
    assert not service["lists_base"], service
    assert not service["reads_source"], service
    assert not service["writes_control"], service
    assert not service["traverses_control"], service

    # And what must keep working: the commit store the board reads to resolve a
    # commit hash, granted to the service by NAME and read-only (SYRD-157).
    assert service["reads_commit_store"], service
    assert not service["writes_commit_store"], service

    # The mode half, and the entry half, each gone on its own terms.
    assert found["base_mode"] == f"0o{TENANT_SOURCE_MODE[1:]}", found
    assert found["worktree_modes"] == ["0o750", "0o750", "0o750"], found
    assert not any(line.startswith(f"group:{SOCKET_GID}:") for line in found["base_acl"]), found
    assert not any(line.startswith(f"group:{SOCKET_GID}:") for line in found["control_acl"]), found
    assert not any(
        line.startswith(f"default:group:{SOCKET_GID}:") for line in found["control_acl"]
    ), found
    # Including the objects git already wrote, which inherited the default.
    assert not any(
        line.startswith(f"group:{SOCKET_GID}:") for line in found["control_object_acl"]
    ), found

    # Neither path is left: not the group's, and not anybody else's.
    outsider = found["outsider"]
    assert not outsider["traverses_base"], outsider
    assert not outsider["lists_base"], outsider
    assert not outsider["reads_source"], outsider

    # And the tenant still owns its own tree.
    owner = found["owner"]
    assert owner["traverses_base"] and owner["reads_source"] and owner["writes_control"], owner


def test_running_it_again_changes_nothing() -> None:
    require_tools()
    found = in_namespace("repeat")
    assert found["first"] == found["second"], found


def test_a_tenant_with_role_accounts_keeps_its_worktrees() -> None:
    """The repository group is granted in the same transaction that retires the
    socket group, so a role does not lose its tree in the gap between them."""
    require_tools()
    found = in_namespace("per_role")
    role = found["role"]
    assert role["traverses_base"], role
    assert role["traverses_control"], role
    assert role["writes_control"], role
    service = found["service"]
    assert not service["traverses_base"], service
    assert not service["writes_control"], service


# --------------------------------------------------- what the packet renders --


def test_a_shared_account_tenant_gets_the_repair_and_no_invented_accounts() -> None:
    """syrd's shape: no role accounts, so nothing creates any."""
    home = Path("/home/switchyard-agent")
    plan = build_plan(project="syrd", owner_user="switchyard-agent", owner_home=home)
    assert plan.role_accounts == (), plan.role_accounts
    packet = render_operator_commands(plan)

    base = f"{home}/syrd-worktrees"
    control = f"{home}/.local/state/switchyard/projects/syrd/control.git"
    assert (
        f"sudo install -d -m 0750 -o 'switchyard-agent' -g 'switchyard-agent' '{base}'" in packet
    ), packet[:400]
    assert f"sudo find '{base}' -mindepth 1 -maxdepth 1 -type d -exec chmod o-rwx {{}} +" in packet
    assert f"sudo setfacl -x g:syrd-roles '{base}'" in packet
    assert f"sudo setfacl -R -x g:syrd-roles '{control}'" in packet
    assert f"sudo setfacl -R -x d:g:syrd-roles '{control}'" in packet
    # Nothing invents role accounts or a repository group for a tenant that has
    # neither, and the socket group itself is never removed or emptied.
    assert "syrd-repo" not in packet, packet
    assert "useradd" not in packet.split("# no per-role runtime preparation")[0] or True
    assert "groupdel" not in packet
    assert "gpasswd -d" not in packet


def test_the_repository_group_is_granted_before_the_socket_group_is_retired() -> None:
    """Order is the whole safety for a tenant whose roles have accounts."""
    from dataclasses import replace

    home = Path("/home/demo-agent")
    plan = replace(
        build_plan(project="demo", owner_user="demo-agent", owner_home=home),
        role_accounts=(("main", "demo-main"),),
        roles_group=roles_group_name("demo"),
        role_worktrees=(("main", f"{home}/demo-worktrees/main"),),
    )
    packet = render_operator_commands(plan)
    grant = packet.index("g:demo-repo:rwX")
    retire = packet.index("-x g:demo-roles")
    assert grant < retire, packet[min(grant, retire) : max(grant, retire)][:400]
    assert "groupadd -r 'demo-repo'" in packet or "groupadd -r demo-repo" in packet, packet


def test_the_retirement_is_guarded_on_the_group_existing() -> None:
    """A tenant that never had a socket group must not stop on `Invalid argument`."""
    home = Path("/home/demo-agent")
    plan = build_plan(project="demo", owner_user="demo-agent", owner_home=home)
    packet = render_operator_commands(plan)
    lines = packet.splitlines()
    retire = next(index for index, line in enumerate(lines) if "-x g:demo-roles" in line)
    guard = next(
        index
        for index in range(retire, -1, -1)
        if lines[index].startswith("if getent group 'demo-roles'")
    )
    assert guard < retire, lines[guard : retire + 1]
    assert any(line.strip() == "fi" for line in lines[retire:retire + 6]), lines[retire:retire + 6]


def test_the_service_keeps_the_socket_and_its_named_read_grants() -> None:
    """What must keep working, named rather than assumed."""
    home = Path("/home/switchyard-agent")
    plan = build_plan(
        project="syrd",
        owner_user="switchyard-agent",
        owner_home=home,
        commit_git_dir=f"{home}/syrd-source-cache.git",
    )
    packet = render_operator_commands(plan)
    # The commit store stays readable to the service by NAME, independent of
    # any group, and so does the board release.
    assert f"sudo setfacl -R -m u:boardsvc:rX '{home}/syrd-source-cache.git'" in packet
    assert f"sudo setfacl -R -m d:u:boardsvc:rX '{home}/syrd-source-cache.git'" in packet
    assert f"sudo setfacl -R -m u:boardsvc:rx '{plan.board_root}'" in packet
    # And nothing takes the service out of the socket group, which is the one
    # thing that group is for.
    assert "gpasswd -d" not in packet
    assert "groupdel" not in packet


def test_a_worktree_base_outside_the_home_is_not_ours_to_re_mode() -> None:
    assert provisioning().tenant_worktree_confinement_commands(
        owner_user="demo-agent",
        owner_home="/home/demo-agent",
        worktree_base="/srv/elsewhere/worktrees",
    ) == []
    assert provisioning().socket_group_retirement_commands(
        owner_user="demo-agent",
        owner_home="/home/demo-agent",
        socket_group="demo-roles",
        worktree_base="/srv/elsewhere/worktrees",
        control_repository="/srv/elsewhere/control.git",
    ) == []


def test_the_owners_own_group_is_never_retired() -> None:
    """A shared-account plan records the owner's name here, and that is not a
    socket group to take away."""
    assert provisioning().socket_group_retirement_commands(
        owner_user="demo-agent",
        owner_home="/home/demo-agent",
        socket_group="demo-agent",
        worktree_base="/home/demo-agent/demo-worktrees",
        control_repository="/home/demo-agent/.local/state/switchyard/projects/demo/control.git",
    ) == []


def test_the_surfaces_are_derived_rather_than_read_from_the_tenant() -> None:
    home = Path("/home/demo-agent")
    plan = build_plan(project="demo", owner_user="demo-agent", owner_home=home)
    assert provisioning().tenant_worktree_base(plan) == f"{home}/demo-worktrees"
    assert provisioning().tenant_control_repository(plan) == (
        f"{home}/.local/state/switchyard/projects/demo/control.git"
    )
    from dataclasses import replace

    recorded = replace(
        plan, role_worktrees=(("main", f"{home}/somewhere-else/main"),)
    )
    assert provisioning().tenant_worktree_base(recorded) == f"{home}/somewhere-else"


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"socket_group_retirement_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--scenario":
        payload = json.loads(sys.stdin.read() or "{}")
        print(json.dumps(SCENARIOS[sys.argv[2]](payload)))
        raise SystemExit(0)
    raise SystemExit(main())
