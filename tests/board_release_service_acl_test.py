#!/usr/bin/env python3
"""SYRD-145: the board service could not reach the release it was about to serve.

A live fresh `switchyard new` got as far as deploying the board and stopped.
The deploy exported an immutable release into the tenant's board tree at mode
0750, then started a canary AS THE SERVICE ACCOUNT, and that account had no
entry anywhere in the tree -- not on the release, not on the board root, not
even traversal on the tenant home. It could not enter its own release, so the
deploy failed and told the User to run `setfacl` by hand.

The grants existed. They were three lines BELOW the deploy in the operator
packet. On a host whose tree already carried them -- every host that had been
provisioned before -- the deploy succeeded and those lines were a no-op, which
is why the ordering survived. On a fresh one the deploy was the first thing to
touch the tree and died before reaching them.

So the order is the fix, and the default ACL is what makes it work: granted on
the board root before anything is exported into it, every release created later
inherits the entry at creation, including releases that do not exist yet.

These cases run the packet's own grant commands against a real tree and ask the
real deploy script whether the service account can traverse the result.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.project_provision import build_plan, render_operator_commands  # noqa: E402

SERVICE = "iam-the-board-service"
PROJECT = "demo"
TENANT = "demo-agent"
#: A uid with no passwd entry. `setfacl` takes it, the deploy script's own
#: traversal check resolves it, and nothing here depends on which accounts this
#: host happens to have -- which is also the point of acceptance 2.
SERVICE_UID = "62145"


def require_tools() -> None:
    for tool in ("setfacl", "getfacl"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"


def plan_for(tmp: Path, *, service_user: str = SERVICE_UID, project: str = PROJECT, tenant: str = TENANT):
    return build_plan(
        project=project,
        owner_user=tenant,
        owner_home=tmp / "home" / tenant,
        service_user=service_user,
        source_repo=ROOT,
    )


def packet_lines(plan) -> list[str]:
    return render_operator_commands(plan).splitlines()


def line_index(lines: list[str], predicate) -> int:
    for index, line in enumerate(lines):
        if predicate(line):
            return index
    raise AssertionError("no line matched")


def board_tree_grants(lines: list[str], board_root: str, owner_home: str) -> list[str]:
    """The packet's own ACL commands for this tenant's board tree.

    Taken from the rendered packet rather than retyped, so a change to what is
    granted changes what runs here. Privilege is the only thing stubbed: these
    run against a tree this process owns, so `sudo` is dropped.
    """
    wanted = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("sudo setfacl") and not stripped.startswith("sudo find"):
            continue
        if board_root not in stripped and f"{owner_home}'" not in stripped and owner_home not in stripped:
            continue
        wanted.append(stripped.removeprefix("sudo "))
    assert wanted, lines
    return wanted


def run_grants(grants: list[str], *, cwd: Path) -> None:
    for command in grants:
        done = subprocess.run(command, shell=True, cwd=cwd, text=True, capture_output=True)
        assert done.returncode == 0, (command, done.stderr)


def build_tree(tmp: Path, plan) -> tuple[Path, Path]:
    """The tenant tree as provisioning leaves it, before any release exists.

    The directories the packet installs, at the modes it installs them with,
    taken from the plan so nothing here is spelled for one project. What is not
    reproduced is ownership: these are this process's, because a test cannot
    chown, and ownership is not what this defect was about.
    """
    home = Path(plan.owner_home)
    board = Path(plan.board_root)
    home.mkdir(parents=True)
    home.chmod(0o700)
    for directory in (Path(plan.asset_dir), Path(plan.frame_dir)):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o775)
    board.mkdir()
    board.chmod(0o755)
    return home, board


def export_release(board: Path, commit: str = "b664b8b7d447b2b513aef8c779ee29c882c15cd9") -> Path:
    """What the deploy does: an immutable release directory at 0750."""
    releases = board / "releases"
    releases.mkdir(exist_ok=True)
    release = releases / commit
    release.mkdir()
    (release / "scripts").mkdir()
    (release / "scripts" / "ticket-board.py").write_text("# release payload\n", encoding="utf-8")
    for path in (releases, release):
        path.chmod(0o750)
    return release


def serviceable(release: Path, account: str, *, home: Path, board: Path, project: str) -> tuple[bool, str]:
    """Ask the real deploy script, the way the canary path asks it."""
    probe = (
        "set -u\n"
        f'source "{ROOT}/scripts/ticket-board-service.sh"\n'
        'ensure_release_root_serviceable "$1"\n'
    )
    done = subprocess.run(
        ["bash", "-c", probe, "probe", str(release)],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "TICKET_BOARD_COMMIT_GIT_DIR": "/nonexistent/commits.git",
            "TICKET_BOARD_OWNER_HOME": str(home),
            "BOARD_ROOT": str(board),
            "BOARD_CANARY_USER": account,
            "TICKET_BOARD_PROJECT": project,
        },
    )
    return done.returncode == 0, done.stdout + done.stderr


def facl(path: Path) -> list[str]:
    return [
        line
        for line in subprocess.run(
            ["getfacl", "-cp", str(path)], text=True, capture_output=True, check=True
        ).stdout.splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------


def test_the_packet_grants_the_board_tree_before_it_deploys_into_it() -> None:
    """The ordering, which is the whole defect.

    Stated against the rendered packet for a project, tenant and service
    account that appear nowhere in the code: the fix is about what provisioning
    does, not about which host it does it on (acceptance 2).
    """
    with tempfile.TemporaryDirectory(prefix="syrd145-order.") as tmp:
        plan = plan_for(Path(tmp), service_user=SERVICE, project="otherproj", tenant="otherproj-agent")
        lines = packet_lines(plan)

    deploy = line_index(lines, lambda l: "ticket-board-service.sh" in l and l.rstrip().endswith("deploy"))
    named = line_index(lines, lambda l: f"setfacl -R -m u:{SERVICE}:rx" in l)
    default = line_index(lines, lambda l: f"d:u:{SERVICE}:rx" in l)
    traversal = line_index(lines, lambda l: f"setfacl -m u:{SERVICE}:--x" in l)
    assert max(named, default, traversal) < deploy, (named, default, traversal, deploy)
    # And the grant is the tenant's board tree, not the tenant's home at large.
    assert plan.board_root in lines[named] and plan.board_root in lines[default]


def walk_packet(plan, tmp: Path) -> tuple[str, str, Path | None]:
    """Run the packet's board-tree steps in the order the packet lists them.

    Not the steps this case would like, in the order it would like: the lines
    come out of the rendered script and are executed in position, and the
    deploy is executed as what it really is -- export a release, then ask the
    real script whether the service account can serve it. A failure there ends
    the walk, because `set -euo pipefail` ends the packet.
    """
    board = Path(plan.board_root)
    home = Path(plan.owner_home)
    grants = set(board_tree_grants(packet_lines(plan), plan.board_root, plan.owner_home))
    for line in packet_lines(plan):
        stripped = line.strip()
        if stripped.removeprefix("sudo ") in grants:
            run_grants([stripped.removeprefix("sudo ")], cwd=tmp)
            continue
        if "ticket-board-service.sh" in stripped and stripped.endswith("deploy"):
            release = export_release(board)
            ok, output = serviceable(
                release, SERVICE_UID, home=home, board=board, project=plan.project
            )
            if not ok:
                return "deploy failed", output, release
            return "deployed", output, release
    raise AssertionError("the packet has no deploy step")


def test_the_packet_run_in_its_own_order_can_serve_what_it_exports() -> None:
    """The live failure, reproduced by running the packet rather than reading it.

    On the code this ticket fixes, the walk stops at the deploy with the User's
    own error: a release exported at 0750 that the service account cannot
    enter. With the grants above the deploy, the same walk exports the same
    release into a tree that already admits the account -- and the release
    inherits the entry rather than being granted one, which is what makes this
    work for releases nobody has exported yet.
    """
    require_tools()
    with tempfile.TemporaryDirectory(prefix="syrd145-walk.") as tmp:
        tmp_path = Path(tmp)
        plan = plan_for(tmp_path)
        board = Path(plan.board_root)
        build_tree(tmp_path, plan)
        outcome, output, release = walk_packet(plan, tmp_path)
        assert outcome == "deployed", output
        assert release is not None
        assert f"user:{SERVICE_UID}:r-x" in facl(release), facl(release)
        assert f"default:user:{SERVICE_UID}:r-x" in facl(board), facl(board)

    # And with no grant at all it is the failure as reported, so the case above
    # is passing for the grant rather than for the mode.
    with tempfile.TemporaryDirectory(prefix="syrd145-ungranted.") as tmp:
        tmp_path = Path(tmp)
        plan = plan_for(tmp_path)
        home, board = build_tree(tmp_path, plan)
        release = export_release(board)
        ok, output = serviceable(release, SERVICE_UID, home=home, board=board, project=PROJECT)
        assert not ok, output
        assert "cannot traverse it" in output, output


def test_the_grant_is_narrow_and_leaves_everything_else_alone() -> None:
    """Acceptance 3 and 4: what it must NOT do while doing that."""
    require_tools()
    with tempfile.TemporaryDirectory(prefix="syrd145-narrow.") as tmp:
        tmp_path = Path(tmp)
        plan = plan_for(tmp_path)
        home, board = build_tree(tmp_path, plan)
        # Something of the tenant's that is not the board tree, and an unrelated
        # ACL entry on the board root that must survive.
        private = home / "credentials"
        private.mkdir()
        private.chmod(0o700)
        before_private = facl(private)
        subprocess.run(["setfacl", "-m", "u:62146:r-x", str(board)], check=True, capture_output=True)

        run_grants(board_tree_grants(packet_lines(plan), plan.board_root, plan.owner_home), cwd=tmp_path)
        release = export_release(board)

        # The unrelated entry is still there.
        assert "user:62146:r-x" in facl(board), facl(board)
        # Nothing outside the board tree was touched, and the home was granted
        # traversal only -- the service account must not read the tenant's home.
        assert facl(private) == before_private, (facl(private), before_private)
        assert f"user:{SERVICE_UID}:--x" in facl(home), facl(home)
        assert not any(line.startswith(f"user:{SERVICE_UID}:r") for line in facl(home)), facl(home)
        # Read and traverse, never write: the service account serves a release,
        # it does not get to change one, and an immutable release that its
        # reader can rewrite is not immutable.
        for path in (board, release):
            entry = [line for line in facl(path) if line.startswith(f"user:{SERVICE_UID}:")]
            assert entry == [f"user:{SERVICE_UID}:r-x"], (path, facl(path))
        assert f"default:user:{SERVICE_UID}:r-x" in facl(board), facl(board)
        # The release stays the tenant's, at the mode the export chose, and
        # `other` is not widened to reach it.
        info = release.stat()
        assert info.st_uid == os.getuid(), info
        assert stat.S_IMODE(info.st_mode) == 0o750, oct(info.st_mode)
        assert "other::---" in facl(release), facl(release)


def test_running_the_grants_again_changes_nothing() -> None:
    """Acceptance 6: rerunning provisioning is how a partial run is completed."""
    require_tools()
    with tempfile.TemporaryDirectory(prefix="syrd145-idempotent.") as tmp:
        tmp_path = Path(tmp)
        plan = plan_for(tmp_path)
        home, board = build_tree(tmp_path, plan)
        grants = board_tree_grants(packet_lines(plan), plan.board_root, plan.owner_home)
        run_grants(grants, cwd=tmp_path)
        release = export_release(board)
        first = (facl(home), facl(board), facl(release))
        run_grants(grants, cwd=tmp_path)
        assert (facl(home), facl(board), facl(release)) == first


def test_a_partly_provisioned_tenant_is_completed_not_recreated() -> None:
    """Acceptance 5, on the state the live failure left behind.

    The account, the repository, the credentials and the journal all exist and
    are the fixture. Completing the missing work must add the grant and touch
    nothing else -- not the bytes, not the inode, not the mode.
    """
    require_tools()
    with tempfile.TemporaryDirectory(prefix="syrd145-resume.") as tmp:
        tmp_path = Path(tmp)
        plan = plan_for(tmp_path)
        home, board = build_tree(tmp_path, plan)
        # What the interrupted run had already done.
        repository = home / "Projects" / PROJECT
        repository.mkdir(parents=True)
        committed = repository / "README.md"
        committed.write_text("# demo\n", encoding="utf-8")
        credential = home / ".claude" / "credentials.json"
        credential.write_text('{"token": "kept"}\n', encoding="utf-8")
        credential.chmod(0o600)
        release = export_release(board)
        before = {
            path: (path.read_bytes(), path.stat().st_ino, stat.S_IMODE(path.stat().st_mode))
            for path in (committed, credential)
        }
        ok, _output = serviceable(release, SERVICE_UID, home=home, board=board, project=PROJECT)
        assert not ok, "the fixture must start in the failed state"

        run_grants(board_tree_grants(packet_lines(plan), plan.board_root, plan.owner_home), cwd=tmp_path)

        # The already-exported release is reached too: the recursive grant
        # covers what exists, and the default covers what comes next.
        ok, output = serviceable(release, SERVICE_UID, home=home, board=board, project=PROJECT)
        assert ok, output
        for path, expected in before.items():
            assert (path.read_bytes(), path.stat().st_ino, stat.S_IMODE(path.stat().st_mode)) == expected, path


def test_the_refusal_names_the_supported_repair_rather_than_a_setfacl_line() -> None:
    """Acceptance 8: nobody is asked to run setfacl.

    The refusal stays -- an unreachable release must not be served -- but what
    it asks for is the thing provisioning does, not a command the User has to
    understand and type against a path they should never have seen.
    """
    require_tools()
    with tempfile.TemporaryDirectory(prefix="syrd145-message.") as tmp:
        tmp_path = Path(tmp)
        plan = plan_for(tmp_path)
        home, board = build_tree(tmp_path, plan)
        release = export_release(board)
        ok, output = serviceable(release, SERVICE_UID, home=home, board=board, project=PROJECT)
    assert not ok
    assert "setfacl" not in output, output
    assert "Re-run this project's provisioning" in output, output
    assert PROJECT in output, output


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"board_release_service_acl_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
