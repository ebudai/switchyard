#!/usr/bin/env python3
"""SYRD-157: the board service held two authorities through one group.

SYRD-156 closed the tenant's checkout and its live review found the rest of it.
The socket group exists so role accounts can talk to the board, and the board
service has to be in it -- that is how the socket reaches the roles. Provisioning
then used that same group as the ACL grantee on the tenant's control repository,
with `rwX` and a default entry, so the service account held read and WRITE over
the tenant's git repository. Membership needed for the first authority silently
conferred the second.

The fix is two groups: the socket group keeps the socket, and a repository group
-- which the service is not in and must never join -- carries git access. The
service is granted, by name, read-only access to the one repository it genuinely
needs: the commit store the board resolves hashes against.

These cases run the product's own commands against real trees and then ask the
kernel, as real separate uids with real supplementary groups, what each account
can do. A user namespace with the invoking user's subuid range mapped supplies
the uids and gids; nothing here needs privilege and nothing here touches the
host.
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

from scripts.ticket_board.project_provision import (  # noqa: E402
    REPOSITORY_COPY_MODE,
    WRITABLE_REPOSITORY_COPY_MODE,
    build_plan,
    commit_store_read_commands,
    render_operator_commands,
    repository_copy_confinement_commands,
    repository_group_commands,
    repository_group_name,
    role_worktree_access_commands,
    roles_group_name,
)

PROJECT = "demo"
OWNER_UID = 1500
SERVICE_UID = 1600
ROLE_UID = 1700
STRANGER_UID = 1800
#: Two groups, which is the entire point of the ticket.
SOCKET_GID = 2600
REPO_GID = 2700
SECRET = "objects and refs the board service has no business writing\n"


# ---------------------------------------------------------------- namespace --


def require_tools() -> None:
    for tool in ("setfacl", "getfacl", "unshare", "setpriv", "install"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"
    probe = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", "true"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, (
        "this host cannot map a second uid into a user namespace "
        f"({probe.stderr.strip()}); what a DIFFERENT account may read and write is the "
        "whole question here, so this fails rather than reporting a pass it did not earn"
    )


def in_namespace(scenario: str) -> dict:
    done = subprocess.run(
        [
            "unshare", "--user", "--map-auto", "--map-root-user", "--mount",
            sys.executable, str(Path(__file__).resolve()), "--scenario", scenario,
        ],
        text=True,
        capture_output=True,
    )
    assert done.returncode == 0, (scenario, done.stdout, done.stderr)
    return json.loads(done.stdout)


def _as(uid: int, argv: list[str], *, groups: list[int] | None = None) -> bool:
    command = ["setpriv", f"--reuid={uid}", f"--regid={uid}"]
    command.append(f"--groups={','.join(str(g) for g in groups)}" if groups else "--clear-groups")
    done = subprocess.run([*command, *argv], capture_output=True)
    return done.returncode == 0


def _run(lines: list[str], cwd: Path) -> None:
    for command in lines:
        stripped = command.strip().removeprefix("sudo ")
        done = subprocess.run(stripped, shell=True, cwd=cwd, capture_output=True, text=True)
        assert done.returncode == 0, (stripped, done.stderr)


def _snapshot(paths: list[Path]) -> list[str]:
    out = []
    for path in paths:
        info = path.stat()
        acl = subprocess.run(
            ["getfacl", "-cp", str(path)], capture_output=True, text=True, check=True
        ).stdout.split()
        out.append(f"{oct(info.st_mode)} {info.st_uid}:{info.st_gid} {' '.join(acl)}")
    return out


# ------------------------------------------------- scenarios (in namespace) --


def _tenant(tmp: Path) -> dict:
    """A migrated tenant as it stands BEFORE this ticket.

    The shapes are the live ones: a control repository the roles write through
    the socket group, a separate source cache the board was pointed at, a
    worktree base, and every one of them world-readable under a home that the
    service may traverse.
    """
    os.chmod(tmp, 0o711)
    home = tmp / "home" / "tenant"
    control = home / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git"
    cache = home / f"{PROJECT}-source-cache.git"
    worktrees = home / f"{PROJECT}-worktrees"
    control.mkdir(parents=True)
    cache.mkdir(parents=True)
    (worktrees / "app").mkdir(parents=True)
    os.chmod(tmp / "home", 0o711)
    for repository in (control, cache, worktrees / "app"):
        (repository / "HEAD").write_text(SECRET, encoding="utf-8")
    for path in (
        home, home / ".local", home / ".local" / "state",
        home / ".local" / "state" / "switchyard",
        home / ".local" / "state" / "switchyard" / "projects",
        home / ".local" / "state" / "switchyard" / "projects" / PROJECT,
        control, control / "HEAD", cache, cache / "HEAD",
        worktrees, worktrees / "app", worktrees / "app" / "HEAD",
    ):
        os.chown(path, OWNER_UID, OWNER_UID)
    os.chmod(home, 0o710)
    for path in (home / ".local", home / ".local" / "state",
                 home / ".local" / "state" / "switchyard",
                 home / ".local" / "state" / "switchyard" / "projects",
                 home / ".local" / "state" / "switchyard" / "projects" / PROJECT,
                 cache, worktrees, worktrees / "app"):
        os.chmod(path, 0o755)
    os.chmod(control, 0o775)
    # What provisioning used to do, and what this ticket takes away: the socket
    # group granted the repository, with a default entry, and the service is a
    # member of that group.
    subprocess.run(["setfacl", "-m", f"u:{SERVICE_UID}:--x", str(home)], check=True)
    subprocess.run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(home)], check=True)
    subprocess.run(["setfacl", "-R", "-m", f"g:{SOCKET_GID}:rwX", str(control)], check=True)
    subprocess.run(["setfacl", "-R", "-m", f"d:g:{SOCKET_GID}:rwX", str(control)], check=True)
    return {"home": home, "control": control, "cache": cache, "worktrees": worktrees}


def _repair_lines(tree: dict, *, selected: Path) -> list[str]:
    """The product's own repair, for this tenant, with the store it selected."""
    home = str(tree["home"])
    lines: list[str] = []
    lines.extend(
        repository_copy_confinement_commands(
            owner_user=str(OWNER_UID),
            owner_home=home,
            repositories=[str(tree["cache"]), str(tree["worktrees"])],
        )
    )
    lines.extend(
        repository_copy_confinement_commands(
            owner_user=str(OWNER_UID),
            owner_home=home,
            repositories=[str(tree["control"])],
            mode=WRITABLE_REPOSITORY_COPY_MODE,
        )
    )
    lines.extend(
        role_worktree_access_commands(
            owner_home=home,
            repository_group=str(REPO_GID),
            worktree_base=str(tree["worktrees"]),
            control_repository=str(tree["control"]),
            retired_groups=(str(SOCKET_GID),),
        )
    )
    lines.extend(
        commit_store_read_commands(
            owner_home=home,
            service_user=str(SERVICE_UID),
            commit_git_dir=str(selected),
        )
    )
    return lines


def _findings(tree: dict, selected: Path, other: Path) -> dict:
    service_groups = [SOCKET_GID]
    role_groups = [SOCKET_GID, REPO_GID]
    return {
        "service_reads_selected": _as(
            SERVICE_UID, ["cat", str(selected / "HEAD")], groups=service_groups
        ),
        "service_lists_selected": _as(SERVICE_UID, ["ls", str(selected)], groups=service_groups),
        "service_writes_selected": _as(
            SERVICE_UID, ["touch", str(selected / "planted")], groups=service_groups
        ),
        "service_reads_other": _as(
            SERVICE_UID, ["cat", str(other / "HEAD")], groups=service_groups
        ),
        "service_writes_other": _as(
            SERVICE_UID, ["touch", str(other / "service-wrote-this")], groups=service_groups
        ),
        "service_traverses_other": _as(
            SERVICE_UID, ["test", "-x", str(other)], groups=service_groups
        ),
        "service_reads_worktree": _as(
            SERVICE_UID, ["cat", str(tree["worktrees"] / "app" / "HEAD")], groups=service_groups
        ),
        "role_reads_control": _as(
            ROLE_UID, ["cat", str(tree["control"] / "HEAD")], groups=role_groups
        ),
        "role_writes_control": _as(
            ROLE_UID, ["touch", str(tree["control"] / "role-wrote-this")], groups=role_groups
        ),
        "role_traverses_worktrees": _as(
            ROLE_UID, ["test", "-x", str(tree["worktrees"])], groups=role_groups
        ),
        "stranger_reads_control": _as(STRANGER_UID, ["cat", str(tree["control"] / "HEAD")]),
        "control_mode": oct(tree["control"].stat().st_mode & 0o7777),
        "cache_mode": oct(tree["cache"].stat().st_mode & 0o7777),
        "worktrees_mode": oct(tree["worktrees"].stat().st_mode & 0o7777),
    }


def scenario_before_the_fix(_payload: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd157-before.") as tmp:
        tree = _tenant(Path(tmp))
        return _findings(tree, tree["cache"], tree["control"])


def scenario_repair_cache_selected(_payload: dict) -> dict:
    """syrd's shape: the board resolves against a separate source cache."""
    with tempfile.TemporaryDirectory(prefix="syrd157-cache.") as tmp:
        tree = _tenant(Path(tmp))
        _run(_repair_lines(tree, selected=tree["cache"]), Path(tmp))
        return _findings(tree, tree["cache"], tree["control"])


def scenario_repair_control_selected(_payload: dict) -> dict:
    """A freshly provisioned tenant's shape: the store IS the control repository."""
    with tempfile.TemporaryDirectory(prefix="syrd157-control.") as tmp:
        tree = _tenant(Path(tmp))
        _run(_repair_lines(tree, selected=tree["control"]), Path(tmp))
        findings = _findings(tree, tree["control"], tree["cache"])
        findings["role_writes_after_service_read_grant"] = _as(
            ROLE_UID,
            ["touch", str(tree["control"] / "still-writable")],
            groups=[SOCKET_GID, REPO_GID],
        )
        return findings


def scenario_repeat(_payload: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd157-repeat.") as tmp:
        tree = _tenant(Path(tmp))
        lines = _repair_lines(tree, selected=tree["cache"])
        _run(lines, Path(tmp))
        first = _snapshot([tree["control"], tree["cache"], tree["worktrees"]])
        _run(lines, Path(tmp))
        second = _snapshot([tree["control"], tree["cache"], tree["worktrees"]])
        return {
            "first": first,
            "second": second,
            "findings": _findings(tree, tree["cache"], tree["control"]),
        }


def scenario_socket(_payload: dict) -> dict:
    """The authority the socket group is for, which must survive all of this."""
    import socket

    with tempfile.TemporaryDirectory(prefix="syrd157-socket.") as tmp:
        os.chmod(tmp, 0o711)
        runtime = Path(tmp) / "run"
        runtime.mkdir()
        os.chown(runtime, SERVICE_UID, SOCKET_GID)
        os.chmod(runtime, 0o750)
        path = runtime / "ticket-board.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(8)
        os.chown(path, SERVICE_UID, SOCKET_GID)
        os.chmod(path, 0o660)
        connect = [
            sys.executable, "-c",
            "import socket,sys; s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM);"
            "s.connect(sys.argv[1]); s.close()",
            str(path),
        ]
        try:
            return {
                "socket_group_member_connects": _as(ROLE_UID, connect, groups=[SOCKET_GID]),
                "repository_group_only_connects": _as(ROLE_UID, connect, groups=[REPO_GID]),
                "stranger_connects": _as(STRANGER_UID, connect),
                "service_connects": _as(SERVICE_UID, connect, groups=[SOCKET_GID]),
            }
        finally:
            server.close()


SCENARIOS = {
    "before_the_fix": scenario_before_the_fix,
    "repair_cache_selected": scenario_repair_cache_selected,
    "repair_control_selected": scenario_repair_control_selected,
    "repeat": scenario_repeat,
    "socket": scenario_socket,
}


# ------------------------------------------------------------------- cases --


def test_the_service_used_to_hold_write_over_the_tenant_repository() -> None:
    """Acceptance 1, as a case that runs: what the inventory found."""
    require_tools()
    found = in_namespace("before_the_fix")
    # Everything the service could reach, and by which of the two mechanisms.
    # World bits, through a home it may traverse: the source cache and every
    # role worktree.
    assert found["service_reads_selected"], found
    assert found["service_reads_worktree"], found
    assert not found["service_writes_selected"], found
    # And the ACL, which is the half no mode change can reach: the control
    # repository was granted to the socket group as `rwX`, and the service is a
    # member of that group, so it could WRITE the tenant's git repository.
    assert found["service_reads_other"], found
    assert found["service_writes_other"], found
    # The home is still the outer gate, and it held: an account with no grant
    # at all never got in. The exposure was to principals holding traversal.
    assert not found["stranger_reads_control"], found


def test_the_service_reads_the_selected_store_and_cannot_write_it() -> None:
    """Acceptance 2 and 5: minimum read for commit resolution, no write anywhere."""
    require_tools()
    for scenario in ("repair_cache_selected", "repair_control_selected"):
        found = in_namespace(scenario)
        assert found["service_reads_selected"], (scenario, found)
        assert found["service_lists_selected"], (scenario, found)
        assert not found["service_writes_selected"], (scenario, found)


def test_the_service_is_denied_every_other_repository_copy() -> None:
    """Acceptance 3 and 5: the store it was given, and nothing beside it."""
    require_tools()
    for scenario in ("repair_cache_selected", "repair_control_selected"):
        found = in_namespace(scenario)
        assert not found["service_reads_other"], (scenario, found)
        assert not found["service_writes_other"], (scenario, found)
        assert not found["service_reads_worktree"], (scenario, found)
        assert found["cache_mode"] in (f"0o{REPOSITORY_COPY_MODE[1:]}", f"0o{WRITABLE_REPOSITORY_COPY_MODE[1:]}"), found
        assert found["worktrees_mode"] == f"0o{REPOSITORY_COPY_MODE[1:]}", found
        assert found["control_mode"] == f"0o{WRITABLE_REPOSITORY_COPY_MODE[1:]}", found


def test_roles_keep_the_git_access_they_had() -> None:
    """Acceptance 2: preserve role Git access -- read AND write on the control repository."""
    require_tools()
    for scenario in ("repair_cache_selected", "repair_control_selected"):
        found = in_namespace(scenario)
        assert found["role_reads_control"], (scenario, found)
        assert found["role_writes_control"], (scenario, found)
        assert found["role_traverses_worktrees"], (scenario, found)
    # Granting the service read on the very repository the roles write must not
    # clip their write: `chmod` would recompute the mask, so the repository the
    # roles write keeps group bits and the grants are made after it.
    control = in_namespace("repair_control_selected")
    assert control["role_writes_after_service_read_grant"], control


def test_nobody_else_gets_in() -> None:
    require_tools()
    for scenario in ("repair_cache_selected", "repair_control_selected"):
        found = in_namespace(scenario)
        assert not found["stranger_reads_control"], (scenario, found)


def test_the_socket_authority_is_untouched() -> None:
    """Acceptance 2 and 5: Board writes still work, and only for the socket group."""
    require_tools()
    found = in_namespace("socket")
    assert found["socket_group_member_connects"], found
    assert found["service_connects"], found
    assert not found["repository_group_only_connects"], found
    assert not found["stranger_connects"], found


def test_running_the_repair_again_changes_nothing() -> None:
    """Acceptance 4: idempotent, ACLs included."""
    require_tools()
    found = in_namespace("repeat")
    assert found["first"] == found["second"], found
    assert not found["findings"]["service_reads_other"], found
    assert found["findings"]["role_writes_control"], found


def test_the_repository_group_is_not_the_socket_group_and_excludes_the_service() -> None:
    """Acceptance 2, stated where the groups are made rather than where they are used."""
    assert repository_group_name(PROJECT) != roles_group_name(PROJECT)
    lines = repository_group_commands(PROJECT, ["demo-agent", "demo-app"])
    joined = "\n".join(lines)
    assert f"groupadd -r '{repository_group_name(PROJECT)}'" in joined, joined
    assert "gpasswd -a 'demo-agent'" in joined and "gpasswd -a 'demo-app'" in joined, joined
    assert "boardsvc" not in joined, joined


def test_the_packet_grants_the_service_read_only_on_the_store_it_configures() -> None:
    """The rendered artifact, so a future change to the wiring has to argue with this."""
    plan = build_plan(
        project=PROJECT,
        owner_user="demo-agent",
        owner_home=Path("/home/demo-agent"),
        service_user="boardsvc",
        source_repo=Path("/home/demo-agent/Projects/demo"),
    )
    lines = render_operator_commands(plan).splitlines()
    store = str(plan.commit_git_dir)
    read = [l for l in lines if f"u:boardsvc:rX {store!r}".replace("'", "'") in l or (f"setfacl -R -m u:boardsvc:rX" in l and store in l)]
    assert read, lines
    for line in lines:
        if "setfacl" not in line or store not in line:
            continue
        assert "u:boardsvc:rwx" not in line.lower(), line
        assert f"g:{roles_group_name(PROJECT)}" not in line, line
    # And the store is closed before it is granted, or the mask would clip it.
    confine = next(i for i, l in enumerate(lines) if "install -d" in l and store in l)
    grant = next(i for i, l in enumerate(lines) if "setfacl -R -m u:boardsvc:rX" in l and store in l)
    assert confine < grant, (confine, grant)


def test_the_grant_is_built_from_the_store_and_not_from_the_release() -> None:
    """The trap that made SYRD-156's first fix a no-op on the live host.

    That packet passed `plan.source_repo` where the tenant's checkout belonged.
    On a provisioned host `source_repo` is the audited RELEASE the artifacts are
    rendered from, outside every tenant home, so the helper answered honestly
    that there was nothing to confine and the fix never ran -- and the fixture
    hid it by passing the checkout in as `source_repo`.

    So this plan is built the way a provisioned host builds one: the release in
    `source_repo`, the tenant's own paths everywhere else. What is granted must
    still be the tenant's commit store.
    """
    plan = build_plan(
        project=PROJECT,
        owner_user="demo-agent",
        owner_home=Path("/home/demo-agent"),
        service_user="boardsvc",
        source_repo=Path("/opt/switchyard/releases/1755eec4832a7a32c96b26ea3d0ac18a7da661c7"),
    )
    store = str(plan.commit_git_dir)
    assert store.startswith("/home/demo-agent/"), store
    granted = [
        line for line in render_operator_commands(plan).splitlines()
        if "setfacl -R -m u:boardsvc:rX" in line
    ]
    assert granted, "the packet granted the service no commit store at all"
    for line in granted:
        assert store in line, line
        assert "/opt/switchyard/releases/" not in line, line
    # And nothing in the packet re-modes or grants anything under the release.
    for line in render_operator_commands(plan).splitlines():
        if "/opt/switchyard/releases/" not in line:
            continue
        assert "setfacl" not in line, line
        assert "install -d" not in line, line


def test_the_repair_artifact_retires_the_old_group_grant() -> None:
    """Acceptance 4: the entry that carried write is taken off, not left beside the new one."""
    commands = role_worktree_access_commands(
        owner_home="/home/tenant",
        repository_group="tenant-repo",
        worktree_base="/home/tenant/worktrees",
        control_repository="/home/tenant/control.git",
        retired_groups=("tenant-roles",),
    )
    joined = "\n".join(commands)
    assert "setfacl -R -x g:tenant-roles '/home/tenant/control.git'" in joined, joined
    assert "setfacl -R -x d:g:tenant-roles '/home/tenant/control.git'" in joined, joined
    assert "setfacl -R -m g:tenant-repo:rwX '/home/tenant/control.git'" in joined, joined
    # Traversal entries are deliberately left alone: a running role pane keeps
    # the groups it started with, and traversal is not the authority at issue.
    assert "setfacl -x g:tenant-roles '/home/tenant/worktrees'" not in joined, joined


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"ticket_board_board_authority_boundary_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--scenario":
        print(json.dumps(SCENARIOS[sys.argv[2]]({})))
        raise SystemExit(0)
    raise SystemExit(main())
