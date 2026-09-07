#!/usr/bin/env python3
"""SYRD-62: a script root runs must not be one the tenant's roles can rewrite.

On the live partial state `syrd-role-accounts.sh` was `root:root` and carried
`user:syrd-director:rwx`, with effective mode 0775, because the control-role
grant is `setfacl -R -m u:<control role>:rwX` plus a default entry over the
whole tenant provision directory -- a directory the tenant owns, so the control
role could also simply unlink the file and put its own there. The documented
operator step then runs that file through sudo.

The two cannot share a directory: the projection has to be writable by the
control role and a root-run script must not be. So root publishes the migration
to its own mirror, the instruction names that copy, and nothing is named without
the whole path to it being checked -- ownership, mode, access control entries
and symlinks, on the file and every directory above it.

The security cases run inside a user namespace, where this process really is
uid 0 and `setfacl` grants are real.
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
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *
from team_launcher_upgrade_cutover_test import (
    _RunningTenant,
    _board_with_marker,
    _declarative_tenant,
)

from scripts.ticket_board.project_provision import (
    ROLE_STAGED_EXECUTABLES,
    acl_write_grants,
    entry_point_module_dependencies,
    role_account_migration_name,
    staged_role_tooling_problems,
    untrusted_root_executable_reasons,
)

PROJECT = "porter"
ROLE_ACCOUNTS = {f"{PROJECT}-{role}" for role in ("designer", "director", "audit", "ops", "app", "main")}
#: A uid that is not this namespace's root, standing in for a role account.
ROLE_UID = 4242
ROLE_GID = 4243


# --------------------------------------------------------------------------
# The whole path, checked as a real root over real access control entries.
# --------------------------------------------------------------------------


def namespace_command(child: list[str], *, euid: int) -> list[str]:
    """The child, always inside a mount namespace of its own.

    Root gets a private mount namespace too, so the tmpfs this mounts over /tmp
    is never left behind in the runner's own namespace (SYRD-60).
    """
    if euid == 0:
        return ["unshare", "--mount", *child]
    return ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", *child]


def test_the_namespace_is_entered_whoever_runs_this() -> None:
    child = ["python3", "x", "--namespace-child"]
    as_root = namespace_command(child, euid=0)
    assert as_root[:2] == ["unshare", "--mount"], as_root
    as_user = namespace_command(child, euid=1000)
    assert "--user" in as_user and "--map-root-user" in as_user, as_user
    for command in (as_root, as_user):
        assert command.index("--mount") < command.index("--namespace-child"), command


def test_the_walk_reaches_the_real_filesystem_root() -> None:
    """With no boundary it keeps going until the anchor, not until the sandbox.

    A check that stopped at whatever directory a caller happened to name would
    miss exactly the ancestor an attacker would use.
    """
    with tempfile.TemporaryDirectory(prefix="trusted-walk.") as raw:
        script = Path(raw) / "run.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        reasons = untrusted_root_executable_reasons(script)
        assert any(reason.startswith("/tmp ") for reason in reasons), reasons
        assert any("world-writable" in reason for reason in reasons), reasons


def _private_tmp() -> None:
    """A tmpfs over /tmp, so every ancestor of a case is one this test made."""
    subprocess.run(["mount", "-t", "tmpfs", "tmpfs", "/tmp"], check=True)
    os.chmod("/tmp", 0o755)


def _trusted_script(where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    script = where / role_account_migration_name(PROJECT)
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _setfacl(*args: str) -> None:
    subprocess.run(["setfacl", *args], check=True)


#: Where the walk stops inside the namespace. `/` there is the host's, and a
#: user namespace reports an unmapped owner for it, so the tmpfs this test
#: mounts is the top of the chain it made and the top of the chain it checks.
#: That the walk goes further when nobody narrows it is proved outside, by
#: test_the_walk_reaches_the_real_filesystem_root.
NAMESPACE_BOUNDARY = Path("/tmp")


def _reasons(path: Path, **kwargs) -> list[str]:
    return untrusted_root_executable_reasons(path, boundary=NAMESPACE_BOUNDARY, **kwargs)


def the_whole_path_is_checked_as_root() -> None:
    """Every case here runs with a real uid 0 and real ACL entries."""
    assert os.geteuid() == 0, "this must run inside the namespace"
    _private_tmp()
    base = Path("/tmp/etc-switchyard/provision") / PROJECT

    # The shape root publishes: root-owned all the way up, no named entries.
    script = _trusted_script(base)
    assert _reasons(script) == [], _reasons(script)

    # A named user grant on the file itself -- the exact live finding.
    _setfacl("-m", f"u:{ROLE_UID}:rwx", str(script))
    reasons = _reasons(script)
    assert any(f"user:{ROLE_UID}:rwx" in reason for reason in reasons), reasons
    _setfacl("-b", str(script))
    assert _reasons(script) == []

    # A named group grant is the same escalation with a different principal.
    _setfacl("-m", f"g:{ROLE_GID}:rwx", str(script))
    assert any(f"group:{ROLE_GID}" in reason for reason in _reasons(script))
    _setfacl("-b", str(script))

    # A default entry on the directory: nothing is writable yet, and every
    # replacement written here will be. That is what made the grant survive the
    # rename each projection does.
    _setfacl("-d", "-m", f"u:{ROLE_UID}:rwx", str(base))
    reasons = _reasons(script)
    assert any(f"default:user:{ROLE_UID}" in reason for reason in reasons), reasons
    _setfacl("-k", str(base))
    assert _reasons(script) == []

    # A writable ancestor needs no grant on the file at all: its owner can
    # unlink the script and put their own at the same name.
    base.parent.chmod(0o775)
    reasons = _reasons(script)
    assert any(str(base.parent) in reason and "writable" in reason for reason in reasons), reasons
    base.parent.chmod(0o755)

    # An ancestor that belongs to somebody else, with nothing else wrong.
    os.chown(base.parent, ROLE_UID, ROLE_GID)
    reasons = _reasons(script)
    assert any(f"owned by uid {ROLE_UID}" in reason for reason in reasons), reasons
    os.chown(base.parent, 0, 0)
    assert _reasons(script) == []

    # Substituted for a symlink pointing at something the role account owns.
    tenant = Path("/tmp/tenant")
    tenant.mkdir()
    os.chown(tenant, ROLE_UID, ROLE_GID)
    theirs = tenant / "theirs.sh"
    theirs.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chown(theirs, ROLE_UID, ROLE_GID)
    script.unlink()
    script.symlink_to(theirs)
    reasons = _reasons(script)
    assert any("symlink" in reason for reason in reasons), reasons
    script.unlink()
    _trusted_script(base)

    # And an unreadable access control list is a refusal, not a pass: a
    # permission that cannot be checked has not been checked.
    def _no_getfacl(_args, **_kwargs):
        raise OSError("getfacl: not found")

    reasons = _reasons(script, runner=_no_getfacl)
    assert any("could not be read" in reason for reason in reasons), reasons

    # The live grant, reproduced through the renderer that emits it: the
    # control-role ACL is applied to a provisioning directory and the published
    # script is not inside it.
    grants, error = acl_write_grants(script)
    assert not error and grants == [], (grants, error)
    print("namespace cases: ok")


# --------------------------------------------------------------------------
# The product: what an upgrade publishes, names, and stages.
# --------------------------------------------------------------------------


def _release(tmp: Path, sha: str) -> Path:
    release = tmp / "opt" / "switchyard" / "releases" / sha
    release.mkdir(parents=True)
    for tree in ("scripts", "skills"):
        shutil.copytree(ROOT / tree, release / tree, symlinks=True)
    (release / ".switchyard-release.json").write_text(
        json.dumps({"commit": sha}) + "\n", encoding="utf-8"
    )
    return release


def _upgrade(config_path: Path, *, exists, runner, source_repo: Path, deploy_ref=None):
    printed: list[str] = []
    original_euid = team_launcher.os.geteuid
    original_exists = team_launcher.local_account_exists
    original_migrate = team_launcher.migrate_declarative_director_onboarding
    original_opener = team_launcher._open_board_url
    original_repo_root = team_launcher._repo_root
    try:
        team_launcher._repo_root = lambda: source_repo
        team_launcher._open_board_url = _board_with_marker(False)
        team_launcher.os.geteuid = lambda: 0
        team_launcher.local_account_exists = lambda account: account in exists
        team_launcher.migrate_declarative_director_onboarding = lambda config, **kwargs: True
        config = team_launcher.load_project_config(PROJECT, config_path)
        result = team_launcher.upgrade_project_command(
            config,
            config_path=config_path,
            source_repo=source_repo,
            deploy_ref=deploy_ref,
            tooling_root=config_path.parent / "tooling",
            runner=runner,
            print_func=printed.append,
        )
    finally:
        team_launcher.os.geteuid = original_euid
        team_launcher.local_account_exists = original_exists
        team_launcher.migrate_declarative_director_onboarding = original_migrate
        team_launcher._open_board_url = original_opener
        team_launcher._repo_root = original_repo_root
    return result, "\n".join(printed)


def _staging_runner(inner, *, staging_root: Path):
    """The host, with the staging the upgrade renders actually carried out."""
    binaries = _staging_sudo_shim(staging_root.parent)

    def call(args, **kwargs):
        argv = [str(part) for part in args]
        if argv[:2] == ["sh", "-c"] and "install -d" in argv[2] and "switchyard" in argv[2]:
            environment = dict(os.environ)
            environment["PATH"] = f"{binaries}:{environment.get('PATH', '')}"
            return subprocess.run(
                ["bash", "-c", argv[2]], env=environment, capture_output=True, text=True
            )
        return inner(argv, **kwargs)

    return call


def _partial_state(tmp: Path) -> tuple[Path, Path, Path, str]:
    """The exact live shape: accounts made, config rolled back, tooling stale.

    Plus the artifact this ticket is about, left where it was found: a tenant-side
    role script carrying a write grant for the control role.
    """
    previous = _release(tmp, "a" * 40)
    selected = _release(tmp, "b" * 40)
    # No tenant board root: this tenant serves its board from the shared
    # install, so the transaction has no release of its own to switch and these
    # cases are about the tooling and the artifact rather than the deploy.
    config_path, _ = _declarative_tenant(tmp)
    # Staged from the release before this one, as a rollout that ran the account
    # script once and then moved on leaves it.
    _stage_role_tooling(tmp, PROJECT, release_root=previous)
    # And the unsafe artifact, exactly as the live host carries it.
    unsafe = config_path.with_name(role_account_migration_name(PROJECT))
    unsafe.write_text("#!/usr/bin/env bash\necho stale\n", encoding="utf-8")
    unsafe.chmod(0o775)
    return config_path, selected, previous, "b" * 40


def test_the_upgrade_publishes_the_script_where_the_control_role_cannot_reach_it() -> None:
    with tempfile.TemporaryDirectory(prefix="trusted-publish.") as raw:
        tmp = Path(raw)
        config_path, selected, _previous, _sha = _partial_state(tmp)
        unsafe = config_path.with_name(role_account_migration_name(PROJECT))
        assert unsafe.exists()
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _upgrade(
                config_path,
                exists=set(),
                runner=_staging_runner(tenant.runner(), staging_root=config_path.parent / "tooling"),
                source_repo=selected,
            )
        assert result == 0, output
        config = team_launcher.load_project_config(PROJECT, config_path)
        published = team_launcher.trusted_role_account_migration_path(config)
        assert published.is_file(), output
        # The instruction names the published copy and nothing else.
        assert _named_migration_path(output) == published, output
        # And the copy a role could rewrite is gone rather than left beside it.
        assert not unsafe.exists(), sorted(p.name for p in config_path.parent.iterdir())
        assert untrusted_root_executable_reasons(
            published, boundary=team_launcher.switchyard_privileged_provision_root()
        ) == []


def test_a_published_script_that_is_not_roots_alone_is_never_handed_over() -> None:
    """Publishing it is not the same as vouching for it."""
    with tempfile.TemporaryDirectory(prefix="trusted-refused.") as raw:
        tmp = Path(raw)
        config_path, selected, _previous, _sha = _partial_state(tmp)
        original = team_launcher.untrusted_root_executable_reasons
        try:
            team_launcher.untrusted_root_executable_reasons = (
                lambda path, **kwargs: [f"{path} grants write through user:porter-director:rwx"]
            )
            with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                result, output = _upgrade(
                    config_path,
                    exists=set(),
                    runner=_staging_runner(
                        tenant.runner(), staging_root=config_path.parent / "tooling"
                    ),
                    source_repo=selected,
                )
        finally:
            team_launcher.untrusted_root_executable_reasons = original
        assert result == 1, output
        assert "grants write through user:porter-director:rwx" in output, output
        assert "not being handed to an operator to run" in output, output
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert journal["phases"]["accounts"]["state"] == "blocked", journal


def test_a_resumed_upgrade_restages_the_tooling_and_reaches_the_transaction() -> None:
    """Accounts already exist, so the account script never runs again."""
    with tempfile.TemporaryDirectory(prefix="trusted-resume.") as raw:
        tmp = Path(raw)
        config_path, selected, previous, sha = _partial_state(tmp)
        unsafe = config_path.with_name(role_account_migration_name(PROJECT))
        assert unsafe.exists(), "the fixture starts from the live shape"
        staging = config_path.parent / "tooling" / PROJECT
        # The state this starts from: the previous release's bundle.
        assert staged_role_tooling_problems(PROJECT, str(previous), staging_root=staging) == []
        assert staged_role_tooling_problems(PROJECT, str(selected), staging_root=staging)
        # Every role account exists and no role runs under its own yet.
        assert all(
            not role.get("run_as_user")
            for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]
        )
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _upgrade(
                config_path,
                exists=ROLE_ACCOUNTS,
                runner=_staging_runner(tenant.runner(), staging_root=config_path.parent / "tooling"),
                source_repo=selected,
                deploy_ref=sha,
            )
        assert result == 0, output
        # The known role-writable root-run path does not survive a successful
        # resume. Nothing published one here -- every account exists, so there
        # is no operator step left -- and it is gone all the same (SYRD-62).
        assert not unsafe.exists(), sorted(p.name for p in config_path.parent.iterdir())
        # Restaged from the selected release, without the account script.
        assert staged_role_tooling_problems(PROJECT, str(selected), staging_root=staging) == []
        assert (staging / ".switchyard-release.json").read_text(encoding="utf-8").strip().endswith("}")
        assert json.loads((staging / ".switchyard-release.json").read_text())["commit"] == sha
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert journal["phases"]["identities"]["state"] == "done", journal


def test_a_legacy_copy_that_cannot_be_removed_stops_the_upgrade() -> None:
    """Reporting success with it still there is what let it survive."""
    with tempfile.TemporaryDirectory(prefix="trusted-legacy.") as raw:
        tmp = Path(raw)
        config_path, selected, _previous, sha = _partial_state(tmp)
        unsafe = config_path.with_name(role_account_migration_name(PROJECT))
        # A name that cannot be unlinked: a directory sits at it.
        unsafe.unlink()
        unsafe.mkdir()
        (unsafe / "keep").write_text("", encoding="utf-8")
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _upgrade(
                config_path,
                exists=ROLE_ACCOUNTS,
                runner=_staging_runner(tenant.runner(), staging_root=config_path.parent / "tooling"),
                source_repo=selected,
                deploy_ref=sha,
            )
        assert result == 1, output
        assert "could not remove the tenant-writable" in output, output
        assert "its control role can write" in output, output
        assert tenant.stops == [], tenant.stops
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert journal["phases"]["artifacts"]["state"] == "blocked", journal


def test_the_legacy_copy_is_removed_as_a_link_never_followed() -> None:
    """A stale copy replaced by a symlink must not make root unlink its target."""
    with tempfile.TemporaryDirectory(prefix="trusted-legacy-link.") as raw:
        tmp = Path(raw)
        config_path, _selected, _previous, _sha = _partial_state(tmp)
        elsewhere = tmp / "somebody-elses-file"
        elsewhere.write_text("not root's to remove\n", encoding="utf-8")
        unsafe = config_path.with_name(role_account_migration_name(PROJECT))
        unsafe.unlink()
        unsafe.symlink_to(elsewhere)
        config = team_launcher.load_project_config(PROJECT, config_path)
        assert team_launcher.remove_untrusted_role_account_migration(
            config, config_path=config_path, print_func=lambda _line: None
        ) == []
        assert not unsafe.exists() and not unsafe.is_symlink()
        assert elsewhere.read_text(encoding="utf-8") == "not root's to remove\n"


def _victim_tree(tmp: Path) -> tuple[Path, Path]:
    """A tree outside the tenant's, holding a file at the very same name.

    Whatever root is asked to unlink, it must not be this one.
    """
    victim = tmp / "victim-parent"
    (victim / "provision").mkdir(parents=True)
    target = victim / "provision" / role_account_migration_name(PROJECT)
    target.write_text("not the tenant's to have removed\n", encoding="utf-8")
    return victim, target


def test_an_ancestor_symlink_cannot_redirect_the_removal() -> None:
    """`O_NOFOLLOW` on the directory refuses only its own last component.

    An ancestor several levels up -- `.switchyard` here -- can be replaced with a
    symlink, and the kernel follows it, so root unlinks the script in whatever
    tree it points at. This is root deleting inside a tree the tenant controls,
    so every component is walked and any symlink among them is a refusal.
    """
    with tempfile.TemporaryDirectory(prefix="trusted-ancestor.") as raw:
        tmp = Path(raw)
        config_path, _selected, _previous, _sha = _partial_state(tmp)
        config = team_launcher.load_project_config(PROJECT, config_path)
        victim, target = _victim_tree(tmp)

        tenant = tmp / "tenant"
        tenant.mkdir()
        (tenant / ".switchyard").symlink_to(victim)
        redirected = tenant / ".switchyard" / "provision" / f"{PROJECT}.json"
        assert redirected.parent.is_dir(), "the symlink resolves, which is the danger"

        problems = team_launcher.remove_untrusted_role_account_migration(
            config, config_path=redirected, print_func=lambda _line: None
        )
        assert problems, "an ancestor symlink must be refused"
        assert ".switchyard" in problems[0] and "symlink" in problems[0], problems
        assert target.is_file(), "root unlinked a file outside the tenant tree"
        assert target.read_text(encoding="utf-8") == "not the tenant's to have removed\n"


def test_an_upgrade_whose_removal_is_refused_blocks_and_stops() -> None:
    """A refusal is not a shrug: the phase records it and nothing later runs."""
    with tempfile.TemporaryDirectory(prefix="trusted-ancestor-upgrade.") as raw:
        tmp = Path(raw)
        config_path, selected, _previous, sha = _partial_state(tmp)
        _victim, target = _victim_tree(tmp)
        # The tenant's own provisioning directory, reached through a symlinked
        # ancestor: everything else about the upgrade is unchanged.
        tenant = tmp / "tenant"
        tenant.mkdir()
        (tenant / ".switchyard").symlink_to(tmp)
        redirected = tenant / ".switchyard" / config_path.name
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant_state:
            result, output = _upgrade(
                redirected,
                exists=ROLE_ACCOUNTS,
                runner=_staging_runner(
                    tenant_state.runner(), staging_root=config_path.parent / "tooling"
                ),
                source_repo=selected,
                deploy_ref=sha,
            )
        assert result == 1, output
        assert "refusing to remove" in output and "symlink" in output, output
        assert "its control role can write" in output, output
        assert tenant_state.stops == [], tenant_state.stops
        assert target.is_file()


def test_a_stale_bundle_stops_the_upgrade_before_any_role_moves() -> None:
    """The roles would come back on one release's hooks against another's board."""
    with tempfile.TemporaryDirectory(prefix="trusted-stale.") as raw:
        tmp = Path(raw)
        config_path, selected, _previous, sha = _partial_state(tmp)
        staging = config_path.parent / "tooling" / PROJECT
        (staging / f"{entry_point_module_dependencies()[0]}.py").unlink()
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            # A host where restaging silently does nothing.
            result, output = _upgrade(
                config_path,
                exists=ROLE_ACCOUNTS,
                runner=tenant.runner(),
                source_repo=selected,
                deploy_ref=sha,
            )
        assert result == 1, output
        assert "is not staged at" in output, output
        assert "would come back on tooling this release did not stage" in output, output
        assert tenant.stops == [], tenant.stops
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert journal["phases"]["artifacts"]["state"] == "blocked", journal


def _named_migration_path(output: str) -> Path:
    """The path the upgrade actually told an operator to run.

    Read out of the instruction rather than recomputed, because where it says to
    go is the whole question.
    """
    name = role_account_migration_name(PROJECT)
    for line in output.splitlines():
        for word in line.replace(",", " ").split():
            if word.endswith(name) and word.startswith("/"):
                return Path(word)
    raise AssertionError(f"no migration artifact was named:\n{output}")


def test_the_transaction_itself_refuses_a_stale_bundle() -> None:
    """`switchyard cutover-roles` reaches this transaction without the upgrade.

    So the check is in the transaction, before anything is stopped, rather than
    only in the phase that usually runs before it.
    """
    with tempfile.TemporaryDirectory(prefix="trusted-transaction.") as raw:
        tmp = Path(raw)
        config_path, selected, _previous, _sha = _partial_state(tmp)
        staging = config_path.parent / "tooling" / PROJECT
        (staging / f"{entry_point_module_dependencies()[0]}.py").unlink()
        printed: list[str] = []
        original_euid = team_launcher.os.geteuid
        original_exists = team_launcher.local_account_exists
        try:
            team_launcher.os.geteuid = lambda: 0
            team_launcher.local_account_exists = lambda account: account in ROLE_ACCOUNTS
            with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                result = team_launcher.cutover_role_identities_command(
                    team_launcher.load_project_config(PROJECT, config_path),
                    config_path=config_path,
                    runner=tenant.runner(),
                    source_repo=selected,
                    tooling_dir=staging,
                    print_func=printed.append,
                )
        finally:
            team_launcher.os.geteuid = original_euid
            team_launcher.local_account_exists = original_exists
        output = "\n".join(printed)
        assert result == 1, output
        assert "is not staged at" in output, output
        assert "Nothing was stopped" in output, output
        assert tenant.stops == [], tenant.stops


def test_the_grant_that_made_this_a_finding_no_longer_covers_the_script() -> None:
    """The projection stays writable by the control role; the script is not there."""
    with tempfile.TemporaryDirectory(prefix="trusted-grant.") as raw:
        tmp = Path(raw)
        config_path, selected, _previous, _sha = _partial_state(tmp)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, output = _upgrade(
                config_path,
                exists=set(),
                runner=_staging_runner(tenant.runner(), staging_root=config_path.parent / "tooling"),
                source_repo=selected,
            )
        config = team_launcher.load_project_config(PROJECT, config_path)
        published = _named_migration_path(output)
        grants = team_launcher.director_control_access_commands_for(config, config_path=config_path)
        assert grants, "this tenant has a control role"
        # Every grant reaches the tenant's provisioning directory. None of them
        # can reach where the script now lives, and the Director keeps the
        # projection it needs to update unprivileged.
        for command in grants:
            assert str(published) not in command, command
            assert str(published.parent) not in command, command
        assert any(str(config_path.parent) in command for command in grants), grants


def test_an_unprivileged_launch_writes_nothing_and_names_the_published_copy() -> None:
    """It runs as the tenant, so a script it wrote would be one a role can rewrite."""
    with tempfile.TemporaryDirectory(prefix="trusted-launch.") as raw:
        tmp = Path(raw)
        config_path, selected, _previous, _sha = _partial_state(tmp)
        (config_path.with_name(role_account_migration_name(PROJECT))).unlink()
        config = team_launcher.load_project_config(PROJECT, config_path)
        path, problems = team_launcher.role_account_migration_instruction(config)
        assert path is None and problems, (path, problems)
        assert "has not been published yet" in problems[0], problems

        # Once root has published one, the same caller names it.
        published = team_launcher.trusted_role_account_migration_path(config)
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        published.chmod(0o755)
        path, problems = team_launcher.role_account_migration_instruction(config)
        assert path == published and problems == [], (path, problems)
        assert not config_path.with_name(role_account_migration_name(PROJECT)).exists()


def main() -> int:
    test_the_namespace_is_entered_whoever_runs_this()
    test_the_walk_reaches_the_real_filesystem_root()
    subprocess.run(
        namespace_command(
            [sys.executable, str(Path(__file__).resolve()), "--namespace-child"],
            euid=os.geteuid(),
        ),
        check=True,
    )
    # The boundary, proved from outside it.
    assert not Path("/tmp/etc-switchyard").exists(), "the namespace leaked"
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_trusted_migration_artifact_test: ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--namespace-child":
        the_whole_path_is_checked_as_root()
    else:
        raise SystemExit(main())
