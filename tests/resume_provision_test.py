#!/usr/bin/env python3
"""SYRD-147: finishing a `switchyard new` that stopped before it registered.

The live recovery ran the supported command and got `unknown project
'testing'`. Everything else existed -- the account, its repository, its
credentials, its desktop policy, its rollout journal, a root-owned provisioning
record and an exported board release -- but the run had failed before writing
the registry entry, and the registry is what every ordinary command resolves a
project through. So the one installation that most needed a supported recovery
was the one no supported command could name.

The recovery has to come from somewhere a tenant cannot write. Root's own
provisioning record is that place: root owns it, the walk to it is checked
component by component, and the identity it names is checked against the
kernel rather than believed. From there the artifacts are rebuilt from a named
audited release -- not patched, and not re-run from the packet that failed,
which was rendered by the release that had the defect.

The privileged half of this suite runs inside a user namespace, where this
process is uid 0 and can own the files root would own.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402

SLUG = "testing"
TENANT = "testing-agent"
TENANT_UID = 62147
SEAM = "SWITCHYARD_PRIVILEGED_PROVISION_ROOT"


def namespaces_available() -> bool:
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "true"], capture_output=True, text=True
    )
    return probe.returncode == 0


def refused(call, expected: str) -> None:
    try:
        result = call()
    except SystemExit as exc:
        assert expected in str(exc), (expected, str(exc))
        return
    raise AssertionError(f"expected a refusal containing {expected!r}; got {result!r}")


# --------------------------------------------------------------------------
# what an unprivileged caller is told
# --------------------------------------------------------------------------


def test_an_unregistered_project_with_a_record_names_its_recovery() -> None:
    """The message the live recovery needed, where the live recovery looked.

    `unknown project` is the right answer for a slug nobody has provisioned. It
    is the wrong answer for one whose installation is on the disk, and the
    difference is a record only root can have written.
    """
    with tempfile.TemporaryDirectory(prefix="syrd147-hint.") as tmp:
        provision = Path(tmp) / "provision"
        (provision / SLUG).mkdir(parents=True)
        (provision / SLUG / "plan.json").write_text(json.dumps({"project": SLUG}), encoding="utf-8")
        registry = Path(tmp) / "registry"
        registry.mkdir()
        with patch.dict(os.environ, {SEAM: str(provision)}):
            refused(
                lambda: launcher._resolve_switchyard_project(
                    SLUG, registry_dir=registry, config_dir=registry
                ),
                f"resume-provision {SLUG}",
            )
            # And a slug nobody provisioned is still simply unknown.
            refused(
                lambda: launcher._resolve_switchyard_project(
                    "nothing-here", registry_dir=registry, config_dir=registry
                ),
                "unknown project",
            )


def test_resuming_says_what_to_run_rather_than_failing_on_uids() -> None:
    """An unprivileged caller is told the one thing they must do differently.

    Every check this command makes reads a path that has to belong to root.
    Running them first as somebody else produces refusals about ownership,
    which is true and useless; the answer they need is `sudo`.
    """
    with tempfile.TemporaryDirectory(prefix="syrd147-sudo.") as tmp:
        provision = Path(tmp) / "provision"
        (provision / SLUG).mkdir(parents=True)
        (provision / SLUG / "plan.json").write_text(json.dumps({"project": SLUG}), encoding="utf-8")
        registry = Path(tmp) / "registry"
        registry.mkdir()
        said: list[str] = []
        with patch.dict(os.environ, {SEAM: str(provision)}):
            status = launcher.switchyard_resume_provision_command(
                SLUG, registry_dir=registry, euid_getter=lambda: 1000, print_func=said.append
            )
        assert status == 1, said
        assert any(f"sudo switchyard resume-provision {SLUG}" in line for line in said), said
        assert not any("uid" in line for line in said), said


def test_a_registered_project_with_nothing_to_resume_is_sent_to_upgrade() -> None:
    """Resuming is for a project whose provisioning did not finish.

    A registered project root holds no record for is not mid-recovery: rebuilding
    what root installs for it is `switchyard upgrade`. A registered project root
    does hold a record for is the other thing -- a recovery that registered it and
    was interrupted before its roles started -- and that one is continued rather
    than refused, which is why the registry entry alone no longer ends this
    (SYRD-155).
    """
    with tempfile.TemporaryDirectory(prefix="syrd147-registered.") as tmp:
        provision = Path(tmp) / "provision"
        provision.mkdir(parents=True)
        registry = Path(tmp) / "registry"
        registry.mkdir()
        (registry / f"{SLUG}.json").write_text("{}", encoding="utf-8")
        said: list[str] = []
        with patch.dict(os.environ, {SEAM: str(provision)}):
            status = launcher.switchyard_resume_provision_command(
                SLUG, registry_dir=registry, euid_getter=lambda: 0, print_func=said.append
            )
        assert status == 1
        assert any(f"switchyard upgrade {SLUG}" in line for line in said), said
        assert any("Nothing was changed" in line for line in said), said


def test_nothing_to_resume_is_said_plainly() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd147-absent.") as tmp:
        provision = Path(tmp) / "provision"
        provision.mkdir()
        registry = Path(tmp) / "registry"
        registry.mkdir()
        said: list[str] = []
        with patch.dict(os.environ, {SEAM: str(provision)}):
            status = launcher.switchyard_resume_provision_command(
                SLUG, registry_dir=registry, euid_getter=lambda: 0, print_func=said.append
            )
        assert status == 1
        assert any("nothing to resume" in line for line in said), said
        assert any("switchyard new" in line for line in said), said


def test_the_command_is_discoverable() -> None:
    assert "resume-provision" in launcher.SWITCHYARD_COMMANDS
    assert "resume-provision" in launcher.switchyard_help_text()
    parser = launcher._build_switchyard_resume_provision_parser()
    args = parser.parse_args([SLUG])
    assert args.project == SLUG and args.source_repo is None
    assert parser.parse_args([SLUG, "--source-repo", "/opt/x"]).source_repo == Path("/opt/x")
    assert "audited release" in parser.format_help()


# --------------------------------------------------------------------------
# the privileged half, inside a user namespace
# --------------------------------------------------------------------------


def fixture(root: Path) -> tuple[Path, Path, Path, Any]:
    """The state the live failure left: a record, an account, a repository.

    Built by the product's own renderer, so the record resumed from is the one
    provisioning writes rather than a hand-made document.
    """
    from scripts.ticket_board.project_provision import build_plan

    release = root / "release"
    shutil.copytree(ROOT / "scripts", release / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    home = root / "home" / TENANT
    (home / "Projects" / SLUG).mkdir(parents=True)
    (home / "Projects" / SLUG / "README.md").write_text("# testing\n", encoding="utf-8")
    (home / ".claude").mkdir()
    credential = home / ".claude" / "credentials.json"
    credential.write_text('{"token": "seeded"}\n', encoding="utf-8")
    credential.chmod(0o600)
    journal = root / "journal" / SLUG
    journal.mkdir(parents=True)
    (journal / "0001-attempt.json").write_text('{"status": "failed"}\n', encoding="utf-8")

    plan = build_plan(
        project=SLUG, owner_user=TENANT, owner_home=home, source_repo=release,
        service_user="boardsvc",
    )
    rendered = launcher.render_privileged_artifacts(plan)
    installed = launcher.install_privileged_artifacts(plan, rendered)
    return release, home, installed, plan


def owner_patches(home: Path):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(launcher, "uid_for_user", lambda name: TENANT_UID if name == TENANT else None))
    stack.enter_context(patch.object(launcher, "home_dir_for_user", lambda name: home if name == TENANT else None))
    real = launcher.pwd.getpwuid
    stack.enter_context(
        patch.object(
            launcher.pwd, "getpwuid",
            side_effect=lambda uid: SimpleNamespace(pw_gid=TENANT_UID) if uid == TENANT_UID else real(uid),
        )
    )
    return stack


def grants_precede_deploy(packet: Path) -> bool:
    lines = packet.read_text(encoding="utf-8").splitlines()
    deploy = next(i for i, l in enumerate(lines) if "ticket-board-service.sh" in l and l.rstrip().endswith("deploy"))
    grant = next(i for i, l in enumerate(lines) if "setfacl -R -m u:boardsvc:rx" in l)
    return grant < deploy


def privileged_cases() -> int:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="syrd147-privileged.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        os.environ[SEAM] = str(root)
        registry = root / "registry"
        registry.mkdir()
        release, home, installed, plan = fixture(root)
        packet = installed / "operator-commands.sh"
        baseline = installed / "plan.json"

        # The shape the live failure left behind.
        assert baseline.is_file() and os.stat(baseline).st_uid == 0, baseline
        assert not (registry / f"{SLUG}.json").exists()

        # These cases are about rebuilding root's artifacts, which happens
        # before the packet has run. What the packet has done is read from the
        # host -- units, a deployed release, a listening board -- so it is
        # answered here instead, and the answer is the one this half is about:
        # not yet. Continuing past it is SYRD-155's own suite.
        unfinished = launcher.PacketCompletion(("the board service is not running",))

        def resume(**kwargs):
            said: list[str] = []
            kwargs.setdefault("completion_reader", lambda _plan: unfinished)
            status = launcher.switchyard_resume_provision_command(
                SLUG, registry_dir=registry, euid_getter=lambda: 0,
                print_func=said.append, **kwargs,
            )
            return status, said

        # The packet as the failed run left it: rendered by the release that had
        # the defect, with the deploy above the grants it needs. Resuming has to
        # replace this, not re-run it.
        stale = packet.read_text(encoding="utf-8").splitlines()
        deploy_at = next(i for i, l in enumerate(stale) if "ticket-board-service.sh" in l and l.rstrip().endswith("deploy"))
        grant_at = next(i for i, l in enumerate(stale) if "setfacl -R -m u:boardsvc:rx" in l)
        deploy_line = stale.pop(deploy_at)
        stale.insert(grant_at, deploy_line)
        packet.write_text("\n".join(stale) + "\n", encoding="utf-8")
        assert not grants_precede_deploy(packet), "the fixture must start with the defective order"

        # 1. Safe discovery and regeneration from a named audited release.
        before = {
            path: (path.read_bytes(), path.stat().st_ino, stat.S_IMODE(path.stat().st_mode))
            for path in (
                home / "Projects" / SLUG / "README.md",
                home / ".claude" / "credentials.json",
            )
        }
        with owner_patches(home):
            status, said = resume(source_repo=release)
        # Not zero: the packet has not run, so the recovery is not finished, and
        # saying otherwise is what let a resumed provision look complete while it
        # was still unregistered and unstarted (SYRD-155).
        assert status == 1, said
        assert grants_precede_deploy(packet), packet.read_text(encoding="utf-8")[:400]
        assert any("operator-commands.sh" in line for line in said), said
        assert any("re-runnable" in line for line in said), said
        assert any("resume-provision" in line and "again" in line for line in said), said
        checks += 1

        # 2. Nothing the tenant owns was touched, by bytes, inode and mode.
        for path, expected in before.items():
            actual = (path.read_bytes(), path.stat().st_ino, stat.S_IMODE(path.stat().st_mode))
            assert actual == expected, path
        assert (root / "journal" / SLUG / "0001-attempt.json").is_file()
        checks += 1

        # 3. Idempotent: the same command again produces the same artifacts.
        snapshot = {p.name: p.read_bytes() for p in installed.iterdir() if p.is_file()}
        with owner_patches(home):
            status, _said = resume(source_repo=release)
        assert status == 1
        assert {p.name: p.read_bytes() for p in installed.iterdir() if p.is_file()} == snapshot
        checks += 1

        # 4. A release that is not one, and one outside root's control.
        with owner_patches(home):
            status, said = resume(source_repo=root / "no-such-release")
            assert status == 1 and any("not a release directory" in l for l in said), said
            empty = root / "empty-release"
            empty.mkdir()
            status, said = resume(source_repo=empty)
            assert status == 1 and any("does not look like a Switchyard release" in l for l in said), said
        checks += 1

        # 5. A record that has been altered: group-writable, then a different
        #    project, then not there at all. Each refuses and changes nothing.
        with owner_patches(home):
            baseline.chmod(0o664)
            status, said = resume(source_repo=release)
            assert status == 1 and any("can write" in l for l in said), said
            baseline.chmod(0o644)

            # Restored byte for byte, so what the snapshot below compares is
            # what the command did and not what this case did to set it up.
            original = baseline.read_bytes()
            recorded = json.loads(original.decode("utf-8"))
            baseline.write_text(json.dumps({**recorded, "project": "somebody-else"}), encoding="utf-8")
            status, said = resume(source_repo=release)
            assert status == 1 and any("records project" in l for l in said), said
            baseline.write_bytes(original)

            # An owner the kernel does not know is not an owner to act for.
            with patch.object(launcher, "uid_for_user", lambda name: None):
                status, said = resume(source_repo=release)
            assert status == 1 and any("not an account on this host" in l for l in said), said
        assert {p.name: p.read_bytes() for p in installed.iterdir() if p.is_file()} == snapshot
        checks += 1

        # 6. A record that would be rebuilt into something else is refused
        #    rather than reconciled: the document is not an authority over what
        #    root installs, and a rebuild that silently moved the board root is
        #    a different installation wearing this one's name.
        with owner_patches(home):
            original = baseline.read_bytes()
            recorded = json.loads(original.decode("utf-8"))
            baseline.write_text(
                json.dumps({**recorded, "board_root": str(root / "somewhere-else")}), encoding="utf-8"
            )
            status, said = resume(source_repo=release)
            baseline.write_bytes(original)
        assert status == 1, said
        assert any("board_root" in l for l in said), said
        assert any("would change what root installs" in l for l in said), said
        assert {p.name: p.read_bytes() for p in installed.iterdir() if p.is_file()} == snapshot
        checks += 1

        # 7. A record whose home disagrees with the kernel is not reconciled.
        with owner_patches(home):
            with patch.object(launcher, "home_dir_for_user", lambda name: root / "elsewhere"):
                status, said = resume(source_repo=release)
        assert status == 1 and any("which one is right is not this command" in l for l in said), said
        assert {p.name: p.read_bytes() for p in installed.iterdir() if p.is_file()} == snapshot
        checks += 1
    return checks


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    if "--privileged-child" in sys.argv:
        checks += privileged_cases()
        print(f"resume_provision_test: privileged child ran {checks} checks")
        return 0
    if namespaces_available():
        done = subprocess.run(
            ["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
            text=True, capture_output=True,
        )
        if done.returncode != 0:
            print(done.stdout + done.stderr)
            return 1
        print(done.stdout.strip())
    else:
        print("resume_provision_test: user namespaces unavailable; privileged half skipped")
    print(f"resume_provision_test: {checks} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
