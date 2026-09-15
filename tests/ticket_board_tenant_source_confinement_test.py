#!/usr/bin/env python3
"""SYRD-156: the board service could read the tenant's source tree.

Testing journal 0012 proved the service account can reach its own immutable
release without write access, and then found that
`sudo -u boardsvc test -r /home/testing-agent/Projects/testing` also succeeded.
It should not: the service serves a release exported into the board tree, and
the tenant's checkout is none of its business.

No ACL granted it. The named entry on the home is `u:boardsvc:--x` under an
`--x` mask, which cannot carry read. The mode bits did it. The home is 0710 so
that traversal has to be granted deliberately, but everything below it was
created 0755 -- and `install -d` is why the parent was the worse half: given
`.../Projects/<project>` it creates both components and applies `-m`, `-o` and
`-g` to the LAST one only, so `Projects` was left at root's umask, owned by
root, above a checkout owned by the tenant.

These cases run the provisioning artifacts' own commands against a real tree and
then ask the kernel, as a second real uid, what that account can do. The uids
come from a user namespace with the invoking user's subuid range mapped, so
nothing here needs privilege and nothing here touches the host: `--map-auto`
makes 1..65536 real uids inside the namespace, `chown` and `setfacl` name them,
and `setpriv` becomes one.
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
    TENANT_SOURCE_MODE,
    build_plan,
    render_operator_commands,
    tenant_source_confinement_commands,
)

PROJECT = "demo"
#: Uids rather than names, for the reason the SYRD-145 suite uses one: nothing
#: here may depend on which accounts this host happens to have, and inside the
#: namespace these are the accounts.
OWNER_UID = 1500
SERVICE_UID = 1600
CONTROL_UID = 1700
SECRET = "tenant source, not the board service's business\n"


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
        f"({probe.stderr.strip()}); the question this suite asks -- what a DIFFERENT "
        "account can read -- cannot be answered without one, so it fails rather than "
        "reporting a pass it did not earn"
    )


def in_namespace(scenario: str, payload: dict | None = None) -> dict:
    """Run one scenario as root inside a user namespace and read back its findings."""
    done = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-auto",
            "--map-root-user",
            "--mount",
            sys.executable,
            str(Path(__file__).resolve()),
            "--scenario",
            scenario,
        ],
        input=json.dumps(payload or {}),
        text=True,
        capture_output=True,
    )
    assert done.returncode == 0, (scenario, done.stdout, done.stderr)
    return json.loads(done.stdout)


# ------------------------------------------------------------------- plans --


def plan_for(home: Path, *, checkout: Path | None = None):
    return build_plan(
        project=PROJECT,
        owner_user=str(OWNER_UID),
        owner_home=home,
        service_user=str(SERVICE_UID),
        source_repo=checkout if checkout is not None else home / "Projects" / PROJECT,
    )


def tenant_tree_lines(plan) -> list[str]:
    """The packet's own commands that build or grant inside this tenant's home.

    Selected from the rendered packet rather than retyped, so a change to what
    provisioning does changes what runs here. Privilege is the only thing
    dropped: the tree belongs to this process inside the namespace.
    """
    home = str(plan.owner_home)
    wanted: list[str] = []
    for line in render_operator_commands(plan).splitlines():
        stripped = line.strip()
        if not stripped.startswith(("sudo install -d", "sudo setfacl", "sudo find")):
            continue
        if home not in stripped:
            continue
        wanted.append(stripped.removeprefix("sudo "))
    assert wanted, "the packet built nothing inside the tenant home"
    return wanted


# ------------------------------------------------- scenarios (in namespace) --


def _tree(tmp: Path) -> tuple[Path, Path, Path]:
    """A tenant home as useradd leaves it, with a checkout already cloned into it."""
    os.chmod(tmp, 0o711)
    home = tmp / "home" / "tenant"
    checkout = home / "Projects" / PROJECT
    checkout.mkdir(parents=True)
    os.chmod(tmp / "home", 0o711)
    (checkout / "secret.txt").write_text(SECRET, encoding="utf-8")
    for path in (home, home / "Projects", checkout, checkout / "secret.txt"):
        os.chown(path, OWNER_UID, OWNER_UID)
    os.chmod(home, 0o710)
    return home, home / "Projects", checkout


def _as(uid: int, argv: list[str]) -> bool:
    done = subprocess.run(
        ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", *argv],
        capture_output=True,
    )
    return done.returncode == 0


def _reads(uid: int, checkout: Path) -> dict:
    return {
        "traverses_home": _as(uid, ["test", "-x", str(checkout.parent.parent)]),
        "traverses_checkout": _as(uid, ["test", "-x", str(checkout)]),
        "reads_checkout": _as(uid, ["test", "-r", str(checkout)]),
        "lists_checkout": _as(uid, ["ls", str(checkout)]),
        "reads_known_file": _as(uid, ["cat", str(checkout / "secret.txt")]),
    }


def _snapshot(paths: list[Path]) -> list[str]:
    out = []
    for path in paths:
        info = path.stat()
        acl = subprocess.run(
            ["getfacl", "-cp", str(path)], capture_output=True, text=True, check=True
        ).stdout.split()
        out.append(f"{oct(info.st_mode)} {info.st_uid}:{info.st_gid} {' '.join(acl)}")
    return out


def _run(lines: list[str], cwd: Path) -> None:
    for command in lines:
        done = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
        assert done.returncode == 0, (command, done.stderr)


def scenario_before_the_fix(_payload: dict) -> dict:
    """The tree exactly as provisioning left it before this ticket.

    Not a guard -- a record of what was reproduced, so the shape the defect had
    is written down in something that runs.
    """
    with tempfile.TemporaryDirectory(prefix="syrd156-before.") as tmp:
        home, projects, checkout = _tree(Path(tmp))
        os.chmod(projects, 0o755)
        os.chown(projects, 0, 0)  # install -d left the parent to root's umask
        os.chmod(checkout, 0o755)
        subprocess.run(["setfacl", "-m", f"u:{SERVICE_UID}:--x", str(home)], check=True)
        return {
            "service": _reads(SERVICE_UID, checkout),
            "projects_mode": oct(projects.stat().st_mode & 0o7777),
            "projects_uid": projects.stat().st_uid,
        }


def scenario_packet(_payload: dict) -> dict:
    """The packet's tenant-tree commands, run in the order the packet lists them."""
    with tempfile.TemporaryDirectory(prefix="syrd156-packet.") as tmp:
        home, projects, checkout = _tree(Path(tmp))
        os.chmod(projects, 0o755)
        os.chown(projects, 0, 0)
        os.chmod(checkout, 0o755)
        plan = plan_for(home)
        _run(tenant_tree_lines(plan), Path(tmp))
        # What the deploy does afterwards: an immutable release at 0750, which
        # the service must still be able to enter and read (SYRD-145).
        release = Path(plan.board_root) / "releases" / "0123456789abcdef"
        (release / "scripts").mkdir(parents=True)
        (release / "scripts" / "ticket-board.py").write_text("# payload\n", encoding="utf-8")
        for path in (release.parent, release):
            os.chmod(path, 0o750)
        assets = Path(plan.asset_dir)
        (assets / "screenshot.png").write_bytes(b"\x89PNG")
        return {
            "service": _reads(SERVICE_UID, checkout),
            "owner": _reads(OWNER_UID, checkout),
            "service_reads_release": _as(
                SERVICE_UID, ["cat", str(release / "scripts" / "ticket-board.py")]
            ),
            "service_lists_release": _as(SERVICE_UID, ["ls", str(release)]),
            "service_writes_release": _as(
                SERVICE_UID, ["touch", str(release / "scripts" / "planted")]
            ),
            "service_reads_asset": _as(SERVICE_UID, ["cat", str(assets / "screenshot.png")]),
            "projects_mode": oct(projects.stat().st_mode & 0o7777),
            "projects_uid": projects.stat().st_uid,
            "checkout_mode": oct(checkout.stat().st_mode & 0o7777),
            "checkout_uid": checkout.stat().st_uid,
        }


def scenario_repeat(_payload: dict) -> dict:
    """Provisioning run twice over its own result changes nothing."""
    with tempfile.TemporaryDirectory(prefix="syrd156-repeat.") as tmp:
        home, projects, checkout = _tree(Path(tmp))
        plan = plan_for(home)
        lines = tenant_tree_lines(plan)
        _run(lines, Path(tmp))
        first = _snapshot([home, projects, checkout])
        _run(lines, Path(tmp))
        second = _snapshot([home, projects, checkout])
        return {"first": first, "second": second, "service": _reads(SERVICE_UID, checkout)}


def scenario_control_role(_payload: dict) -> dict:
    """A named traversal grant still traverses after the tree is closed.

    The control role is granted `--x` on the checkout by name so it can reach
    its own configuration below. That grant is traversal, and traversal is what
    it keeps -- it never named read, and the read it used to get came from the
    world bits this ticket removes.
    """
    with tempfile.TemporaryDirectory(prefix="syrd156-control.") as tmp:
        home, projects, checkout = _tree(Path(tmp))
        plan = plan_for(home)
        _run(tenant_tree_lines(plan), Path(tmp))
        for path in (projects, checkout):
            subprocess.run(["setfacl", "-m", f"u:{CONTROL_UID}:--x", str(path)], check=True)
        subprocess.run(["setfacl", "-m", f"u:{CONTROL_UID}:--x", str(home)], check=True)
        below = checkout / "state"
        below.mkdir()
        os.chmod(below, 0o755)
        (below / "config.json").write_text("{}\n", encoding="utf-8")
        return {
            "control": _reads(CONTROL_UID, checkout),
            "control_reaches_below": _as(CONTROL_UID, ["cat", str(below / "config.json")]),
        }


def scenario_fresh_creation(_payload: dict) -> dict:
    """The checkout as `switchyard new` creates it, before any packet has run.

    The argv the launcher builds, executed by the real `install`, so the modes
    come from the product rather than from this file. Nothing has granted the
    service account anything yet -- the point is that the window between
    creating the tree and granting traversal is not a window at all.
    """
    import scripts.team_launcher as team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd156-fresh.") as tmp:
        os.chmod(tmp, 0o711)
        home = Path(tmp) / "home" / "tenant"
        home.mkdir(parents=True)
        os.chmod(Path(tmp) / "home", 0o711)
        os.chown(home, OWNER_UID, OWNER_UID)
        os.chmod(home, 0o710)
        checkout = home / "Projects" / PROJECT
        for argv in team_launcher._owner_project_install_commands(
            str(OWNER_UID), checkout, owner_home=home
        ):
            done = subprocess.run(argv, capture_output=True, text=True)
            assert done.returncode == 0, (argv, done.stderr)
        (checkout / "secret.txt").write_text(SECRET, encoding="utf-8")
        os.chown(checkout / "secret.txt", OWNER_UID, OWNER_UID)
        subprocess.run(["setfacl", "-m", f"u:{SERVICE_UID}:--x", str(home)], check=True)
        return {
            "service": _reads(SERVICE_UID, checkout),
            "owner": _reads(OWNER_UID, checkout),
            "projects_mode": oct((home / "Projects").stat().st_mode & 0o7777),
            "projects_uid": (home / "Projects").stat().st_uid,
            "checkout_mode": oct(checkout.stat().st_mode & 0o7777),
        }


def scenario_outside_home(_payload: dict) -> dict:
    """A checkout kept outside the tenant home is not ours to re-mode."""
    with tempfile.TemporaryDirectory(prefix="syrd156-outside.") as tmp:
        home, _projects, _checkout = _tree(Path(tmp))
        elsewhere = Path(tmp) / "srv" / PROJECT
        elsewhere.mkdir(parents=True)
        os.chmod(Path(tmp) / "srv", 0o755)
        os.chmod(elsewhere, 0o755)
        plan = plan_for(home, checkout=elsewhere)
        _run(tenant_tree_lines(plan), Path(tmp))
        return {"mode": oct(elsewhere.stat().st_mode & 0o7777)}


SCENARIOS = {
    "before_the_fix": scenario_before_the_fix,
    "packet": scenario_packet,
    "repeat": scenario_repeat,
    "control_role": scenario_control_role,
    "fresh_creation": scenario_fresh_creation,
    "outside_home": scenario_outside_home,
}


# ------------------------------------------------------------------- cases --


def test_the_tree_provisioning_used_to_leave_is_readable_by_the_service() -> None:
    """Acceptance 1: what was reproduced, and which half of it did the damage."""
    require_tools()
    found = in_namespace("before_the_fix")
    assert found["projects_mode"] == "0o755", found
    assert found["projects_uid"] == 0, found
    service = found["service"]
    assert service["traverses_home"], service
    assert service["reads_checkout"], service
    assert service["lists_checkout"], service
    assert service["reads_known_file"], service


def test_provisioning_closes_the_tenant_tree_to_the_service() -> None:
    """Acceptance 2: the service keeps its release and assets and loses the source."""
    require_tools()
    found = in_namespace("packet")
    service = found["service"]
    assert service["traverses_home"], service
    assert not service["reads_checkout"], service
    assert not service["lists_checkout"], service
    assert not service["reads_known_file"], service
    assert not service["traverses_checkout"], service
    assert found["checkout_mode"] == f"0o{TENANT_SOURCE_MODE[1:]}", found
    assert found["projects_mode"] == f"0o{TENANT_SOURCE_MODE[1:]}", found
    assert found["projects_uid"] == OWNER_UID, found
    assert found["checkout_uid"] == OWNER_UID, found
    # The board tree is what it serves, and it still serves it.
    assert found["service_reads_release"], found
    assert found["service_lists_release"], found
    assert not found["service_writes_release"], found
    assert found["service_reads_asset"], found
    # And the tenant still owns its own tree in every sense.
    owner = found["owner"]
    assert owner["reads_checkout"] and owner["lists_checkout"] and owner["reads_known_file"], owner


def test_running_provisioning_again_changes_nothing() -> None:
    """Acceptance 5: repeated provisioning is a no-op, including the ACLs."""
    require_tools()
    found = in_namespace("repeat")
    assert found["first"] == found["second"], found
    assert not found["service"]["reads_checkout"], found


def test_a_named_traversal_grant_still_traverses() -> None:
    """Acceptance 3: what was granted by name is preserved; only the world bits go."""
    require_tools()
    found = in_namespace("control_role")
    control = found["control"]
    assert control["traverses_home"], control
    assert control["traverses_checkout"], control
    assert found["control_reaches_below"], found
    assert not control["reads_checkout"], control
    assert not control["lists_checkout"], control


def test_a_fresh_checkout_is_closed_the_moment_it_exists() -> None:
    """Acceptance 2, at creation: there is no interval where the tree is open."""
    require_tools()
    found = in_namespace("fresh_creation")
    assert found["projects_mode"] == f"0o{TENANT_SOURCE_MODE[1:]}", found
    assert found["checkout_mode"] == f"0o{TENANT_SOURCE_MODE[1:]}", found
    assert found["projects_uid"] == OWNER_UID, found
    service = found["service"]
    assert service["traverses_home"], service
    assert not service["reads_checkout"], service
    assert not service["lists_checkout"], service
    assert not service["reads_known_file"], service
    owner = found["owner"]
    assert owner["reads_checkout"] and owner["reads_known_file"], owner


def test_a_checkout_outside_the_home_is_left_alone() -> None:
    require_tools()
    assert in_namespace("outside_home")["mode"] == "0o755"


def test_every_directory_is_named_rather_than_created_on_the_way_past() -> None:
    """The parent is the half `install -d` would have left at root's umask."""
    commands = tenant_source_confinement_commands(
        owner_user="tenant",
        owner_home="/home/tenant",
        source_repo="/home/tenant/Projects/demo",
    )
    assert commands == [
        "sudo install -d -m 0750 -o 'tenant' -g 'tenant' '/home/tenant/Projects'",
        "sudo install -d -m 0750 -o 'tenant' -g 'tenant' '/home/tenant/Projects/demo'",
    ], commands
    deeper = tenant_source_confinement_commands(
        owner_user="tenant",
        owner_home="/home/tenant",
        source_repo="/home/tenant/a/b/c",
    )
    assert [line.rsplit(" ", 1)[-1] for line in deeper] == [
        "'/home/tenant/a'",
        "'/home/tenant/a/b'",
        "'/home/tenant/a/b/c'",
    ], deeper


def test_a_path_that_only_shares_the_home_prefix_is_refused_rather_than_ignored() -> None:
    """`/home/tenant-old/...` is a different account's tree, and re-moding it would be theft.

    Silently emitting nothing would be safe and unreadable; the refusal names
    the path, so a misconfigured checkout is a stopped provisioning run rather
    than a tenant quietly left open.
    """
    try:
        tenant_source_confinement_commands(
            owner_user="tenant",
            owner_home="/home/tenant",
            source_repo="/home/tenant-old/Projects/demo",
        )
    except ValueError as exc:
        assert "/home/tenant-old/Projects/demo" in str(exc), exc
        assert "/home/tenant" in str(exc), exc
    else:
        raise AssertionError("a prefix-only path was accepted")
    # `..` is refused rather than normalized, because normalizing would resolve
    # through directories the tenant controls. A trailing slash is not an escape
    # and is deliberately still accepted.
    for bad in ("/home/tenant/../other", "home/tenant"):
        try:
            tenant_source_confinement_commands(
                owner_user="tenant", owner_home=bad, source_repo=f"{bad}/Projects/demo"
            )
        except ValueError:
            continue
        raise AssertionError(f"the owner home {bad!r} was accepted unnormalized")
    assert tenant_source_confinement_commands(
        owner_user="tenant", owner_home="/home/tenant/", source_repo="/home/tenant/Projects/demo"
    ), "a trailing slash is not an escape and must not stop provisioning"


def test_the_tree_is_closed_before_anything_is_granted_a_way_through_the_home() -> None:
    plan = plan_for(Path("/home/tenant"))
    lines = render_operator_commands(plan).splitlines()
    confine = [i for i, line in enumerate(lines) if "/home/tenant/Projects'" in line]
    traversal = [
        i
        for i, line in enumerate(lines)
        if f"setfacl -m u:{SERVICE_UID}:--x '/home/tenant'" in line
    ]
    assert confine and traversal, lines
    assert max(confine) < min(traversal), (confine, traversal)


def test_no_grant_names_the_service_account_on_the_tenant_tree() -> None:
    """Acceptance 2 stated as an absence, so a future grant there has to argue for itself."""
    plan = plan_for(Path("/home/tenant"))
    for line in render_operator_commands(plan).splitlines():
        if "setfacl" not in line or f"u:{SERVICE_UID}" not in line:
            continue
        assert "/home/tenant/Projects" not in line, line


def test_the_repair_artifact_carries_the_same_commands() -> None:
    """Acceptance 4: an existing tenant is repaired by the supported path, not by hand."""
    import scripts.team_launcher as team_launcher

    config = _repair_config()
    artifact = team_launcher.render_role_account_migration(config)
    for command in tenant_source_confinement_commands(
        owner_user="tenant",
        owner_home="/home/tenant",
        source_repo="/home/tenant/Projects/demo",
    ):
        assert command in artifact, (command, artifact)


def test_the_fresh_checkout_is_created_closed() -> None:
    """The window before the packet runs is closed too, and every directory is named."""
    import scripts.team_launcher as team_launcher

    commands = team_launcher._owner_project_install_commands(
        "tenant",
        Path("/home/tenant/Projects/demo"),
        owner_home=Path("/home/tenant"),
    )
    assert commands == [
        ["install", "-d", "-m", TENANT_SOURCE_MODE, "-o", "tenant", "-g", "tenant", "/home/tenant/Projects"],
        ["install", "-d", "-m", TENANT_SOURCE_MODE, "-o", "tenant", "-g", "tenant", "/home/tenant/Projects/demo"],
    ], commands
    # Without a home to measure against, the leaf is still created closed.
    leaf_only = team_launcher._owner_project_install_commands("tenant", Path("/srv/demo"))
    assert leaf_only == [
        ["install", "-d", "-m", TENANT_SOURCE_MODE, "-o", "tenant", "-g", "tenant", "/srv/demo"]
    ], leaf_only


def _repair_config():
    import scripts.team_launcher as team_launcher

    config = team_launcher.ProjectConfig.__new__(team_launcher.ProjectConfig)
    object.__setattr__(config, "project", PROJECT)
    object.__setattr__(config, "run_as_user", "tenant")
    object.__setattr__(config, "repository", Path("/home/tenant/Projects/demo"))
    object.__setattr__(config, "control_repository", Path("/home/tenant/.local/state/control.git"))
    object.__setattr__(config, "roles", ())
    object.__setattr__(config, "worktree_base", Path("/home/tenant/worktrees"))
    return config


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"ticket_board_tenant_source_confinement_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--scenario":
        payload = json.loads(sys.stdin.read() or "{}")
        print(json.dumps(SCENARIOS[sys.argv[2]](payload)))
        raise SystemExit(0)
    raise SystemExit(main())
