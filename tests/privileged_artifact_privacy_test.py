#!/usr/bin/env python3
"""SYRD-176: root's provisioning artifacts are root's to read.

`install_privileged_artifacts` says it publishes into "a directory only root
can reach" and then chmod'd that directory 0755 on every run. During SYRD-146
(syrd rollout journal 0084) that turned syrd's directory from 0700 to 0755 and
left the plan, the operator packet, the database and workflow SQL, the workflow
record and the publication remote readable by every local account -- including
the board service's. Together those describe the whole authority model: which
accounts exist, what each role may call, where the socket and the database are.

The testing tenant stayed 0700, because the last writer to touch it happened to
be one that closes the directory. Which writer ran last is not an access policy,
so all of them now go through one place, and that place is checked here against
the kernel: a real service account, in a real namespace, is asked to read what
it must not.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.project_provision import build_plan  # noqa: E402
from resume_provision_test import SEAM, namespaces_available  # noqa: E402

PROJECT = "porter"
OWNER_UID = 1500
SERVICE_UID = 1600
OTHER_UID = 1700

#: What the directory holds that no account it describes may read.
SECRETS = ("plan.json", "operator-commands.sh", f"{PROJECT}-database.sql", f"{PROJECT}-workflow.sql")


def require_tools() -> None:
    for tool in ("unshare", "setpriv"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"


# --------------------------------------------------------------------------
# what the modes are, as values
# --------------------------------------------------------------------------


def test_the_modes_are_the_least_each_consumer_needs() -> None:
    assert launcher.PRIVILEGED_PROVISION_DIR_MODE == 0o700
    assert launcher.PRIVILEGED_ARTIFACT_MODE == 0o600
    assert launcher.PRIVILEGED_EXECUTABLE_ARTIFACT_MODE == 0o700
    # Root runs the packets; root reads everything else. Nobody else does either.
    assert launcher.privileged_artifact_mode("operator-commands.sh") == 0o700
    assert launcher.privileged_artifact_mode("plan.json") == 0o600
    assert launcher.privileged_artifact_mode(f"{PROJECT}-workflow.sql") == 0o600
    assert launcher.privileged_artifact_mode(f"{PROJECT}-ticket-board.service") == 0o600
    for mode in (
        launcher.PRIVILEGED_PROVISION_DIR_MODE,
        launcher.PRIVILEGED_ARTIFACT_MODE,
        launcher.PRIVILEGED_EXECUTABLE_ARTIFACT_MODE,
    ):
        assert not mode & 0o077, oct(mode)


def test_an_open_directory_is_named_as_a_problem_and_a_closed_one_is_not() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd176-detect.") as tmp:
        root = Path(tmp)
        target = root / PROJECT
        target.mkdir(mode=0o755)
        os.environ[SEAM] = str(root)
        try:
            problems = launcher.privileged_provision_privacy_problems(PROJECT)
            assert len(problems) == 1, problems
            assert "0755" in problems[0] and "readable beyond root" in problems[0], problems
            target.chmod(0o700)
            assert launcher.privileged_provision_privacy_problems(PROJECT) == []
            # A tenant root has never heard of is not a problem to report.
            assert launcher.privileged_provision_privacy_problems("absent") == []
        finally:
            os.environ.pop(SEAM, None)


def test_a_caller_who_is_not_really_root_still_records_through_the_seam() -> None:
    """The documented seam is how these paths are exercised off a host.

    Through it "root" is the caller's own uid, and a caller cannot chown a
    directory away from itself. Insisting on the chown turned every record an
    upgrade takes into `Operation not permitted` and stopped the upgrade -- the
    ownership is checked by reading the directory back, and the attempt to set
    it is not what the invariant rests on.
    """
    with tempfile.TemporaryDirectory(prefix="syrd176-seam.") as tmp:
        root = Path(tmp)
        os.environ[SEAM] = str(root)
        try:
            target = root / PROJECT
            target.mkdir(mode=0o755)
            repaired = launcher.ensure_privileged_provision_dir(target)
            assert repaired and "closed" in repaired[0], repaired
            assert stat.S_IMODE(target.lstat().st_mode) == 0o700
            assert launcher.privileged_provision_privacy_problems(PROJECT) == []
            # Again, and it neither raises nor reports a repair it did not make.
            assert launcher.ensure_privileged_provision_dir(target) == []
            assert stat.S_IMODE(target.lstat().st_mode) == 0o700
        finally:
            os.environ.pop(SEAM, None)


# --------------------------------------------------------------------------
# the boundary, against a real tree and a real account
# --------------------------------------------------------------------------


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


def as_account(uid: int, argv: list[str]) -> bool:
    """Whether that account can do this. The kernel decides, not a mode reading."""
    return subprocess.run(
        ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", *argv],
        capture_output=True,
    ).returncode == 0


def plan_for(home: Path, *, per_role: bool = False):
    plan = build_plan(
        project=PROJECT,
        owner_user=str(OWNER_UID),
        owner_home=home,
        service_user=str(SERVICE_UID),
        source_repo=ROOT,
    )
    if not per_role:
        return plan
    from dataclasses import replace

    from scripts.ticket_board.project_provision import roles_group_name

    accounts = (("director", f"{PROJECT}-director"), ("main", f"{PROJECT}-main"))
    return replace(
        plan,
        role_accounts=accounts,
        roles_group=roles_group_name(PROJECT),
        role_worktrees=tuple(
            (role, f"{home}/{PROJECT}-worktrees/{role}") for role, _ in accounts
        ),
    )


def install(plan, *, privileged_root: Path | None = None) -> Path:
    rendered = launcher.render_privileged_artifacts(plan)
    return launcher.install_privileged_artifacts(plan, rendered, privileged_root=privileged_root)


def modes(target: Path) -> dict[str, int]:
    return {
        entry.name: stat.S_IMODE(entry.lstat().st_mode)
        for entry in target.iterdir()
        if entry.is_file()
    }


def name_the_namespaces_accounts(home: Path, root: Path) -> None:
    """Give this namespace names for the three uids the fixture uses.

    An upgrade resolves a tenant's home from the account that owns its
    provision directory, and a fresh user namespace has no name for any uid in
    it. The host's account database plus three lines, bind-mounted, is what
    makes `uid 1500 lives at <home>` true here the way it is on a host --
    nothing in the product is told about it.
    """
    passwd = root / "passwd"
    passwd.write_text(
        Path("/etc/passwd").read_text(encoding="utf-8")
        + f"{OWNER_UID}:x:{OWNER_UID}:{OWNER_UID}:tenant:{home}:/bin/sh\n"
        + f"{SERVICE_UID}:x:{SERVICE_UID}:{SERVICE_UID}:service:/nonexistent:/bin/sh\n"
        + f"{OTHER_UID}:x:{OTHER_UID}:{OTHER_UID}:other:/nonexistent:/bin/sh\n",
        encoding="utf-8",
    )
    group = root / "group"
    group.write_text(
        Path("/etc/group").read_text(encoding="utf-8")
        + "".join(f"{uid}:x:{uid}:\n" for uid in (OWNER_UID, SERVICE_UID, OTHER_UID)),
        encoding="utf-8",
    )
    run(["mount", "--make-rprivate", "/"])
    run(["mount", "--bind", str(passwd), "/etc/passwd"])
    run(["mount", "--bind", str(group), "/etc/group"])
    home.mkdir(parents=True)
    os.chown(home, OWNER_UID, OWNER_UID)
    os.chmod(home, 0o710)
    import pwd as pwd_module

    assert pwd_module.getpwnam(str(OWNER_UID)).pw_dir == str(home)


def privileged_cases() -> int:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="syrd176-privileged.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        home = root / "home" / "tenant"
        name_the_namespaces_accounts(home, root)
        provision_root = root / "etc-switchyard"
        os.environ[SEAM] = str(provision_root)
        plan = plan_for(home)

        # 1. Fresh provisioning: the directory is root's alone, every artifact
        #    inside it is too, and the kernel agrees.
        target = install(plan)
        assert stat.S_IMODE(target.lstat().st_mode) == 0o700, oct(target.lstat().st_mode)
        assert target.lstat().st_uid == 0 and target.lstat().st_gid == 0
        published = modes(target)
        assert published, target
        for name, mode in published.items():
            assert mode == (0o700 if name.endswith(".sh") else 0o600), (name, oct(mode))
        for name in SECRETS:
            assert (target / name).is_file(), (name, sorted(published))
            assert not as_account(SERVICE_UID, ["cat", str(target / name)]), name
            assert not as_account(OTHER_UID, ["cat", str(target / name)]), name
        assert not as_account(SERVICE_UID, ["ls", str(target)])
        assert not as_account(SERVICE_UID, ["test", "-x", str(target)])
        # The shared root above it is still traversable: this closes a tenant's
        # directory, not every root-owned path on the host.
        assert stat.S_IMODE(provision_root.lstat().st_mode) == 0o755
        checks += 1

        # 2. The legacy state, and the repair. This is syrd's shape: the
        #    directory opened to 0755 by an earlier run, everything in it
        #    readable, and a supported path closing it again.
        target.chmod(0o755)
        for name in SECRETS:
            (target / name).chmod(0o755 if name.endswith(".sh") else 0o644)
        assert as_account(SERVICE_UID, ["cat", str(target / "plan.json")]), (
            "the fixture must start open, or the repair proves nothing"
        )
        assert launcher.privileged_provision_privacy_problems(PROJECT)
        repaired = launcher.ensure_privileged_provision_dir(target)
        assert repaired and "closed" in repaired[0] and "0755" in repaired[0], repaired
        assert stat.S_IMODE(target.lstat().st_mode) == 0o700
        assert not as_account(SERVICE_UID, ["cat", str(target / "plan.json")])
        assert launcher.privileged_provision_privacy_problems(PROJECT) == []
        checks += 1

        # 3. The artifacts themselves are closed again by the path that
        #    rewrites them, not left at whatever the legacy run gave them.
        install(plan)
        for name, mode in modes(target).items():
            assert mode == (0o700 if name.endswith(".sh") else 0o600), (name, oct(mode))
        checks += 1

        # 4. Idempotent: again changes nothing and reports no repair.
        before = (stat.S_IMODE(target.lstat().st_mode), modes(target))
        assert launcher.ensure_privileged_provision_dir(target) == []
        install(plan)
        assert (stat.S_IMODE(target.lstat().st_mode), modes(target)) == before
        checks += 1

        # 5. Every writer, not just the installer. Each of these used to create
        #    or reopen the directory itself.
        for writer in (
            lambda: launcher.record_tenant_config_path(PROJECT, home / "config.json"),
            lambda: launcher.write_workflow_record(PROJECT, {"stages": [], "transitions": []}),
        ):
            target.chmod(0o755)
            written = writer()
            assert stat.S_IMODE(target.lstat().st_mode) == 0o700, writer
            assert stat.S_IMODE(written.lstat().st_mode) == 0o600, (written, writer)
            assert not as_account(SERVICE_UID, ["cat", str(written)]), written
        checks += 1

        # 6. Partial artifacts: a directory holding only some of them is
        #    repaired and completed, not refused.
        (target / "plan.json").unlink()
        target.chmod(0o755)
        install(plan)
        assert (target / "plan.json").is_file()
        assert stat.S_IMODE(target.lstat().st_mode) == 0o700
        checks += 1

        # 7. Unrelated root-controlled entries are left exactly as they are.
        #    syrd's directory holds a decade of hand-written operator scripts,
        #    and closing the directory is not a licence to rewrite them.
        legacy = target / "SYRD-126-operator.sh"
        legacy.write_text("#!/bin/sh\necho hand written\n", encoding="utf-8")
        legacy.chmod(0o755)
        legacy_body = legacy.read_bytes()
        rollback = target / "authority-rollback"
        rollback.mkdir()
        (rollback / "keep").write_text("kept\n", encoding="utf-8")
        target.chmod(0o755)
        install(plan)
        assert legacy.read_bytes() == legacy_body
        assert stat.S_IMODE(legacy.lstat().st_mode) == 0o755, "an unrelated entry was rewritten"
        assert (rollback / "keep").read_text(encoding="utf-8") == "kept\n"
        # And they are unreachable anyway, because the directory above them is.
        assert not as_account(SERVICE_UID, ["cat", str(legacy)])
        # The mode repair names what it closes, and an operator's own script is
        # not one of them.
        legacy.chmod(0o755)
        closed = launcher.close_privileged_artifacts(
            target, launcher.render_privileged_artifacts(plan)
        )
        assert closed == [], closed
        assert stat.S_IMODE(legacy.lstat().st_mode) == 0o755
        (target / "plan.json").chmod(0o644)
        closed = launcher.close_privileged_artifacts(
            target, launcher.render_privileged_artifacts(plan)
        )
        assert closed and "plan.json" in closed[0] and "SYRD-126" not in closed[0], closed
        assert stat.S_IMODE((target / "plan.json").lstat().st_mode) == 0o600
        assert stat.S_IMODE(legacy.lstat().st_mode) == 0o755
        checks += 1

        # 8. A per-role tenant is the same boundary.
        per_role_root = root / "etc-per-role"
        os.environ[SEAM] = str(per_role_root)
        per_role_target = install(plan_for(home, per_role=True))
        assert stat.S_IMODE(per_role_target.lstat().st_mode) == 0o700
        assert all(
            mode == (0o700 if name.endswith(".sh") else 0o600)
            for name, mode in modes(per_role_target).items()
        )
        assert not as_account(SERVICE_UID, ["ls", str(per_role_target)])
        os.environ[SEAM] = str(provision_root)
        checks += 1

        # 9. A path with spaces and shell metacharacters in it is a path.
        odd_root = root / "etc dir; rm -rf . $(touch pwned)"
        os.environ[SEAM] = str(odd_root)
        odd_target = install(plan)
        assert stat.S_IMODE(odd_target.lstat().st_mode) == 0o700
        assert not (root / "pwned").exists() and not (Path.cwd() / "pwned").exists()
        assert not as_account(SERVICE_UID, ["ls", str(odd_target)])
        os.environ[SEAM] = str(provision_root)
        checks += 1

        # 10. A symlinked directory is refused, and what it points at is not
        #     touched -- neither its mode nor its contents.
        elsewhere = root / "elsewhere"
        elsewhere.mkdir(mode=0o755)
        (elsewhere / "someone-elses.txt").write_text("not root's\n", encoding="utf-8")
        link_root = root / "etc-link"
        link_root.mkdir()
        (link_root / PROJECT).symlink_to(elsewhere)
        os.environ[SEAM] = str(link_root)
        try:
            install(plan)
            raise AssertionError("a symlinked provision directory was accepted")
        except SystemExit as exc:
            assert "symlink" in str(exc), exc
        assert stat.S_IMODE(elsewhere.lstat().st_mode) == 0o755
        assert (elsewhere / "someone-elses.txt").read_text(encoding="utf-8") == "not root's\n"
        assert not (elsewhere / "plan.json").exists()
        os.environ[SEAM] = str(provision_root)
        checks += 1

        # 11. A directory somebody else owns is refused, and nothing is written
        #     into it -- root's plan there would be its owner's to read.
        stolen_root = root / "etc-stolen"
        stolen = stolen_root / PROJECT
        stolen.mkdir(parents=True)
        os.chown(stolen, OWNER_UID, OWNER_UID)
        before_entries = sorted(entry.name for entry in stolen.iterdir())
        os.environ[SEAM] = str(stolen_root)
        try:
            install(plan)
            raise AssertionError("a tenant-owned provision directory was accepted")
        except SystemExit as exc:
            assert "owned by uid" in str(exc) and "Nothing" in str(exc), exc
        assert sorted(entry.name for entry in stolen.iterdir()) == before_entries
        assert stolen.lstat().st_uid == OWNER_UID
        os.environ[SEAM] = str(provision_root)
        checks += 1

        # 12. The tenant's own copies are not this boundary's business. The
        #     artifacts a tenant owns, and the board release it runs, are
        #     exactly as they were.
        tenant_dir = home / "Projects" / PROJECT / ".switchyard" / "provision"
        tenant_dir.mkdir(parents=True)
        tenant_copy = tenant_dir / "operator-commands.sh"
        tenant_copy.write_text("#!/bin/sh\necho tenant\n", encoding="utf-8")
        tenant_copy.chmod(0o755)
        for path in (tenant_dir, tenant_copy):
            os.chown(path, OWNER_UID, OWNER_UID)
        release = home / f"{PROJECT}-ticketboard-live" / "current"
        release.mkdir(parents=True)
        (release / "board").write_text("running\n", encoding="utf-8")
        os.chmod(release, 0o755)
        before_tenant = (
            stat.S_IMODE(tenant_dir.lstat().st_mode),
            stat.S_IMODE(tenant_copy.lstat().st_mode),
            tenant_copy.read_bytes(),
            tenant_copy.lstat().st_uid,
            stat.S_IMODE(release.lstat().st_mode),
            (release / "board").read_bytes(),
        )
        target.chmod(0o755)
        install(plan)
        launcher.ensure_privileged_provision_dir(target)
        assert (
            stat.S_IMODE(tenant_dir.lstat().st_mode),
            stat.S_IMODE(tenant_copy.lstat().st_mode),
            tenant_copy.read_bytes(),
            tenant_copy.lstat().st_uid,
            stat.S_IMODE(release.lstat().st_mode),
            (release / "board").read_bytes(),
        ) == before_tenant
        # The tenant still reads its own copy; that is what it is for.
        assert as_account(OWNER_UID, ["cat", str(tenant_copy)])
        checks += 1

        # 13. An unprivileged caller can tell "root has closed this" from
        #     "nothing is published", and says so rather than sending an
        #     operator to regenerate what is already waiting.
        # The product's own function, run by an account that really is not
        # root. The source tree lives under a home this account cannot read, so
        # a readable copy of it is what the child imports -- the code is the
        # same file, and the directories it is asked about are the real ones.
        readable = root / "lib"
        shutil.copytree(ROOT / "scripts", readable / "scripts")
        run(["chmod", "-R", "a+rX", str(readable)])
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from scripts import team_launcher as launcher; "
            "print(launcher._privileged_directory_is_closed(__import__('pathlib').Path(sys.argv[2])))"
        )
        for directory, expected in (
            (target, "True"),
            (provision_root, "False"),
            (root / "not-there", "False"),
        ):
            done = subprocess.run(
                ["setpriv", f"--reuid={SERVICE_UID}", f"--regid={SERVICE_UID}", "--clear-groups",
                 sys.executable, "-c", probe, str(readable), str(directory)],
                # Without the seam, so "root" means uid 0 to the child the way
                # it does on a host. The directories it is asked about are root's.
                env={name: value for name, value in os.environ.items() if name != SEAM},
                capture_output=True, text=True,
            )
            assert done.returncode == 0, done.stderr
            assert done.stdout.strip() == expected, (directory, done.stdout, done.stderr)
        checks += 1

        # 14. The path an operator actually runs. syrd's artifacts are current
        #     and its directory is open, which is the one combination the
        #     rendered-bytes comparison would have skipped entirely: nothing to
        #     refresh, so nothing repaired. `switchyard upgrade` closes it, says
        #     so, and rewrites none of the artifacts (acceptance 2).
        from scripts.ticket_board.project_provision import write_artifacts

        tenant_provision = root / "upgrade-tenant"
        tenant_provision.mkdir()
        repository = root / "upgrade-repository"
        repository.mkdir()
        upgrade_plan = plan_for(home)
        write_artifacts(upgrade_plan, tenant_provision, enable_owner_linger=False)
        config_path = launcher.write_new_project_launcher_artifacts(
            upgrade_plan, tenant_provision, repository=repository, print_func=lambda _text: None
        )
        # `switchyard new` writes the control repository and worktree base at
        # /home/<account> by convention. This tenant's home is the fixture's, so
        # the two are pointed at it -- the paths, and nothing else about the
        # configuration, are what this shapes.
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["control_repository"] = str(
            home / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git"
        )
        raw["worktree_base"] = str(home / f"{PROJECT}-worktrees")
        config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        run(["chown", "-R", f"{OWNER_UID}:{OWNER_UID}", str(tenant_provision)])
        config = launcher.load_project_config(PROJECT, config_path)

        upgrade_root = root / "etc-upgrade"
        os.environ[SEAM] = str(upgrade_root)
        upgrade_target = install(upgrade_plan)
        published_before = {
            entry.name: entry.read_bytes() for entry in upgrade_target.iterdir() if entry.is_file()
        }
        # The legacy state exactly: the directory and the artifacts in it as the
        # version that wrote them left them.
        upgrade_target.chmod(0o755)
        for entry in upgrade_target.iterdir():
            if entry.is_file():
                entry.chmod(0o755 if entry.name.endswith(".sh") else 0o644)
        assert as_account(SERVICE_UID, ["cat", str(upgrade_target / "plan.json")])

        def quiet(args, **kwargs):
            if args[0] == "chown":
                return subprocess.run(args, **kwargs)
            return subprocess.CompletedProcess(args, 0, "", "")

        said: list[str] = []
        outcome = launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=config_path, runner=quiet, print_func=said.append
        )
        assert "already current" in outcome.message, (outcome.message, said)
        assert "closed" in outcome.message and "0755" in outcome.message, outcome.message
        assert outcome.changed, outcome
        assert stat.S_IMODE(upgrade_target.lstat().st_mode) == 0o700
        assert not as_account(SERVICE_UID, ["cat", str(upgrade_target / "plan.json")])
        assert {
            entry.name: entry.read_bytes() for entry in upgrade_target.iterdir() if entry.is_file()
        } == published_before, "an artifact was rewritten by a mode repair"
        # The artifacts this render owns are closed too, not left at 0644
        # inside a closed directory.
        for name, mode in modes(upgrade_target).items():
            assert mode == (0o700 if name.endswith(".sh") else 0o600), (name, oct(mode))
        assert "artifact(s)" in outcome.message, outcome.message
        # And running it again is quiet: there is nothing left to close.
        second = launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=config_path, runner=quiet, print_func=said.append
        )
        assert "closed" not in second.message, second.message
        assert not second.changed, second
        os.environ[SEAM] = str(provision_root)
        checks += 1

        os.environ.pop(SEAM, None)
    return checks


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    if "--privileged-child" in sys.argv:
        checks += privileged_cases()
        print(f"privileged_artifact_privacy_test: privileged child ran {checks} checks")
        return 0
    require_tools()
    if namespaces_available():
        done = subprocess.run(
            ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", sys.executable,
             str(Path(__file__).resolve()), "--privileged-child"],
            text=True, capture_output=True,
        )
        if done.returncode != 0:
            print(done.stdout + done.stderr)
            return 1
        print(done.stdout.strip())
    else:
        print("privileged_artifact_privacy_test: user namespaces unavailable; privileged half skipped")
    print(f"privileged_artifact_privacy_test: {checks} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
