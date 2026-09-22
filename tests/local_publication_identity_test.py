#!/usr/bin/env python3
"""A tenant that does not publish to GitHub has no GitHub identity to manage.

Live mefp upgrade (SYRD-229): mefp publishes to a local bare repository,
`/data/git/fixpatch`, yet the upgrade ran the owner-GitHub-identity workflow,
warned that no GitHub key was selected and sent the operator to
set-owner-identity -- which then recorded `id_ed25519` in both plan authorities,
wrote a managed `github.com` block, and reported a GitHub authentication
failure for a forge mefp never uses.

Built on the owner-identity suite's harness, so the upgrade and the repair are
the real commands against a fixture owner home. The publication remote is set
where root records it, never in the tenant's git config.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import owner_identity_preserved_across_upgrades_test as identity  # noqa: E402
from owner_identity_preserved_across_upgrades_test import (  # noqa: E402
    DEFAULT_KEY,
    identity_reported_as,
    identity_scripts,
    owner_home_with,
    run_upgrade,
    tenant_with_owner_home,
    trusted_owner,
)
from scripts import team_launcher  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    GITHUB_IDENTITY_BEGIN,
    compose_ssh_config,
    github_identity_block,
    owner_github_block_removal_commands,
    publication_uses_github,
)
from scripts.ticket_board.publication_boundary import publish_remote_registration_path  # noqa: E402

LOCAL_REMOTE = "/data/git/fixpatch"
GITHUB_REMOTE = "git@github.com:example/porter.git"
UNRELATED = "Host bastion.invalid\n    User someone\n"


def _pin_remote(remote: str) -> Path:
    """Where root records the publication destination, as an earlier run would."""
    path = publish_remote_registration_path(
        "porter", team_launcher.switchyard_privileged_provision_root()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(remote + "\n", encoding="utf-8")
    return path


def test_remotes_are_classified_by_host() -> None:
    for remote, expected in (
        (LOCAL_REMOTE, False),
        ("file:///data/git/fixpatch.git", False),
        ("../fixpatch.git", False),
        ("ssh://git@gitlab.example/o/r.git", False),
        (GITHUB_REMOTE, True),
        ("https://github.com/example/porter.git", True),
        ("git@github-switchyard:ebudai/switchyard.git", True),
        ("git@forge:example/porter.git", False),
        ("", None),
    ):
        assert publication_uses_github(remote) is expected, (remote, expected)
    # A recorded alias makes an otherwise unknown host GitHub.
    assert publication_uses_github("git@forge:example/porter.git", recorded_host_alias="forge")


def test_a_local_tenant_upgrade_manages_no_github_identity() -> None:
    """mefp's shape: the upgrade must not select, configure or check anything."""
    with tempfile.TemporaryDirectory(prefix="local-identity.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEFAULT_KEY,))
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        _pin_remote(LOCAL_REMOTE)
        with identity_reported_as(False) as asked:
            output, runner = run_upgrade(config_path)
        assert identity_scripts(runner) == [], identity_scripts(runner)
        assert asked == [], f"GitHub authentication was checked for a local tenant: {asked}"
        assert f"publishes to {LOCAL_REMOTE}, not GitHub" in output, output
        for phrase in ("no GitHub key", "set-owner-identity", "GitHub identity was left untouched",
                       "register", "Permission denied"):
            assert phrase not in output, (phrase, output)
        assert not (home / ".ssh" / "config").exists(), "a local tenant was given an ssh config"


def test_a_local_tenant_with_a_stray_identity_is_pointed_at_the_cleanup() -> None:
    with tempfile.TemporaryDirectory(prefix="local-identity-stray.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEFAULT_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home, recorded_key=DEFAULT_KEY)
        _pin_remote(LOCAL_REMOTE)
        with identity_reported_as(False) as asked:
            output, runner = run_upgrade(config_path)
        assert asked == [] and identity_scripts(runner) == [], (asked, identity_scripts(runner))
        assert "set-owner-identity porter --clear" in output, output
        assert "--key-name" not in output, output


def test_a_github_tenant_keeps_its_identity_behaviour() -> None:
    """The other side: a genuine GitHub tenant is still selected and checked."""
    with tempfile.TemporaryDirectory(prefix="github-identity.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEFAULT_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home, recorded_key=DEFAULT_KEY)
        _pin_remote(GITHUB_REMOTE)
        with identity_reported_as(True) as asked:
            output, runner = run_upgrade(config_path)
        assert identity_scripts(runner), "a GitHub tenant's identity was no longer provisioned"
        assert asked == [DEFAULT_KEY], asked
        assert "not GitHub" not in output, output



# --- the pin: `upgrade --publish-remote` is what records it ----------------
#
# Live mefp after af51547: `set-owner-identity mefp --clear --dry-run` refused
# for want of a root-pinned remote, right after an upgrade given
# `--publish-remote`. Only the retired publication boundary ever wrote the pin.
# These go through the real upgrade, not the writer.


def _registration() -> Path:
    return publish_remote_registration_path(
        "porter", team_launcher.switchyard_privileged_provision_root()
    )


def upgrade_with_remote(config_path: Path, remote: str, *, dry_run: bool = False):
    real = team_launcher.upgrade_project_command

    def with_remote(*args, **kwargs):
        return real(*args, publish_remote=remote, **kwargs)

    team_launcher.upgrade_project_command = with_remote
    try:
        with identity_reported_as(False) as asked:
            output, runner = run_upgrade(config_path, dry_run=dry_run)
    finally:
        team_launcher.upgrade_project_command = real
    return output, runner, asked


def test_an_upgrade_given_a_local_remote_records_it_for_everything_after() -> None:
    with tempfile.TemporaryDirectory(prefix="local-identity-pin.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEFAULT_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home, recorded_key=DEFAULT_KEY)
        assert not _registration().exists(), "the fixture started with a pin"
        output, runner, asked = upgrade_with_remote(config_path, LOCAL_REMOTE)
        assert _registration().read_text(encoding="utf-8").strip() == LOCAL_REMOTE, output
        assert f"recorded porter's publication remote as {LOCAL_REMOTE}" in output, output
        assert asked == [] and identity_scripts(runner) == [], (asked, identity_scripts(runner))
        # And what reads the pin later now finds it -- the live failure.
        with trusted_owner(home) as root_plan:
            _root_plan_with_identity(root_plan)
            result, cleared, _runner = clear(config_path, dry_run=True)
        assert result == 0, cleared
        assert "not established" not in cleared, cleared
        assert f"publishes to {LOCAL_REMOTE}, not GitHub" in cleared, cleared


def test_a_dry_run_upgrade_says_it_would_record_the_remote_and_does_not() -> None:
    with tempfile.TemporaryDirectory(prefix="local-identity-pin-dry.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEFAULT_KEY,))
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        output, runner, asked = upgrade_with_remote(config_path, LOCAL_REMOTE, dry_run=True)
        assert f"would record porter's publication remote as {LOCAL_REMOTE}" in output, output
        assert not _registration().exists(), "the dry run recorded the remote"
        # The dry run still judges by the remote it was given.
        assert f"publishes to {LOCAL_REMOTE}, not GitHub" in output, output
        assert asked == [] and identity_scripts(runner) == []


def test_a_remote_that_is_not_one_is_not_recorded() -> None:
    with tempfile.TemporaryDirectory(prefix="local-identity-pin-bad.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEFAULT_KEY,))
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        output, _runner, _asked = upgrade_with_remote(config_path, "/data/git/fix patch\nevil")
        assert "refusing to record" in output, output
        assert not _registration().exists(), _registration().read_text(encoding="utf-8")


def test_the_same_remote_again_leaves_the_record_alone() -> None:
    with tempfile.TemporaryDirectory(prefix="local-identity-pin-again.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEFAULT_KEY,))
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        upgrade_with_remote(config_path, LOCAL_REMOTE)
        stamp = _registration().stat().st_mtime_ns
        output, _runner, _asked = upgrade_with_remote(config_path, LOCAL_REMOTE)
        assert "already recorded as" in output, output
        assert _registration().stat().st_mtime_ns == stamp


# --- the cleanup ------------------------------------------------------------


def _stray(tmp_path: Path):
    """What the erroneous flow left behind: a recorded key and alias, and a block."""
    home = owner_home_with(tmp_path, keys=(DEFAULT_KEY,), selected=DEFAULT_KEY)
    config_path, _plan = tenant_with_owner_home(tmp_path, home, recorded_key=DEFAULT_KEY)
    tenant_plan = config_path.parent / "plan.json"
    data = json.loads(tenant_plan.read_text(encoding="utf-8"))
    data["owner_github_host_alias"] = "github-fixpatch"
    tenant_plan.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return home, config_path, tenant_plan


def _root_plan_with_identity(root_plan: Path) -> None:
    data = json.loads(root_plan.read_text(encoding="utf-8"))
    data["owner_github_key_name"] = DEFAULT_KEY
    data["owner_github_host_alias"] = "github-fixpatch"
    data["commit_git_dir"] = "/data/git/stellaris-fixpatch.git"
    root_plan.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear(config_path: Path, **kwargs):
    printed: list[str] = []
    runner = identity.FakeRunner()
    config = team_launcher.load_project_config("porter", config_path)
    real = team_launcher.os.geteuid
    try:
        team_launcher.os.geteuid = lambda: 0
        result = team_launcher.clear_owner_github_identity_command(
            config, config_path=config_path, runner=runner, print_func=printed.append, **kwargs
        )
    finally:
        team_launcher.os.geteuid = real
    return result, "\n".join(printed), runner


def _removal_ran(runner) -> bool:
    return any(
        len(call) >= 3 and call[:2] == ["sh", "-c"] and "sed" in call[2]
        for call in runner.calls
    )


def test_a_dry_run_cleanup_describes_it_and_writes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-clear-dry.") as tmp:
        home, config_path, tenant_plan = _stray(Path(tmp))
        remote_record = _pin_remote(LOCAL_REMOTE)
        with trusted_owner(home) as root_plan:
            _root_plan_with_identity(root_plan)
            before = (root_plan.read_bytes(), tenant_plan.read_bytes(), remote_record.read_bytes())
            result, output, runner = clear(config_path, dry_run=True)
            after = (root_plan.read_bytes(), tenant_plan.read_bytes(), remote_record.read_bytes())
        assert result == 0, output
        assert before == after, "the dry run wrote a plan or the remote record"
        assert str(root_plan) in output and str(tenant_plan) in output, output
        assert output.count("would have owner_github_key_name and owner_github_host_alias cleared") == 2, output
        assert "would have Switchyard's managed GitHub block removed" in output, output
        assert not _removal_ran(runner), "the dry run removed the block"


def test_the_cleanup_clears_both_authorities_and_only_the_managed_block() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-clear.") as tmp:
        home, config_path, tenant_plan = _stray(Path(tmp))
        remote_record = _pin_remote(LOCAL_REMOTE)
        with trusted_owner(home) as root_plan:
            _root_plan_with_identity(root_plan)
            remote_before = remote_record.read_bytes()
            result, output, runner = clear(config_path)
            documents = [json.loads(p.read_text(encoding="utf-8")) for p in (root_plan, tenant_plan)]
        assert result == 0, output
        for document in documents:
            assert document["owner_github_key_name"] == "", document
            assert document["owner_github_host_alias"] == "", document
        # Not the concerns next to it: the remote and the commit store stand.
        assert remote_record.read_bytes() == remote_before
        assert documents[0]["commit_git_dir"] == "/data/git/stellaris-fixpatch.git"
        assert _removal_ran(runner), "the managed block was not removed"


def test_a_failed_second_write_leaves_both_authorities_as_they_were() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-clear-rollback.") as tmp:
        home, config_path, tenant_plan = _stray(Path(tmp))
        _pin_remote(LOCAL_REMOTE)
        real_write = team_launcher.write_plan_no_follow

        def fails_on_the_tenant_copy(document, body):
            if document.path == tenant_plan and body != document.raw:
                return f"{document.path} could not be replaced (simulated)"
            return real_write(document, body)

        team_launcher.write_plan_no_follow = fails_on_the_tenant_copy
        try:
            with trusted_owner(home) as root_plan:
                _root_plan_with_identity(root_plan)
                before = (root_plan.read_bytes(), tenant_plan.read_bytes())
                result, output, runner = clear(config_path)
                after = (root_plan.read_bytes(), tenant_plan.read_bytes())
        finally:
            team_launcher.write_plan_no_follow = real_write
        assert result == 1, output
        assert "was put back as it was" in output, output
        assert before == after, "a failed cleanup left the two authorities disagreeing"
        assert not _removal_ran(runner), "the block was removed although the plans were not cleared"


def test_the_cleanup_is_refused_for_a_github_tenant() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-clear-github.") as tmp:
        home, config_path, tenant_plan = _stray(Path(tmp))
        _pin_remote(GITHUB_REMOTE)
        with trusted_owner(home) as root_plan:
            _root_plan_with_identity(root_plan)
            before = (root_plan.read_bytes(), tenant_plan.read_bytes())
            result, output, runner = clear(config_path)
            assert (root_plan.read_bytes(), tenant_plan.read_bytes()) == before
        assert result == 1 and "is GitHub" in output, output
        assert not _removal_ran(runner)


def test_the_cleanup_is_refused_when_no_remote_is_established() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-clear-unknown.") as tmp:
        home, config_path, tenant_plan = _stray(Path(tmp))
        with trusted_owner(home) as root_plan:
            _root_plan_with_identity(root_plan)
            result, output, _runner = clear(config_path)
        assert result == 1 and "not established" in output, output


def _run_removal_as_this_user(home: Path) -> None:
    """The removal command, minus the `sudo -u <owner>` it runs under."""
    argv = shlex.split(owner_github_block_removal_commands("porter-owner", str(home))[0])
    assert argv[:5] == ["sudo", "-u", "porter-owner", "sh", "-c"], argv
    subprocess.run(["sh", "-c", argv[5]], check=True)


def test_the_removal_keeps_every_other_stanza_byte_for_byte() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-block-removal.") as tmp:
        home = Path(tmp) / "home"
        (home / ".ssh").mkdir(parents=True)
        config = home / ".ssh" / "config"
        config.write_text(
            compose_ssh_config(UNRELATED, github_identity_block(str(home), key_name=DEFAULT_KEY))
            + "Host after.invalid\n    Port 2222\n",
            encoding="utf-8",
        )
        _run_removal_as_this_user(home)
        text = config.read_text(encoding="utf-8")
        assert GITHUB_IDENTITY_BEGIN not in text and "github.com" not in text, text
        assert UNRELATED in text and "Host after.invalid\n    Port 2222\n" in text, text
        # And a config without the block is not rewritten at all.
        stamp = config.stat().st_mtime_ns
        _run_removal_as_this_user(home)
        assert config.stat().st_mtime_ns == stamp and config.read_text(encoding="utf-8") == text


def main() -> int:
    tests = sorted(name for name in globals() if name.startswith("test_"))
    for name in tests:
        globals()[name]()
    print(f"local_publication_identity_test: {len(tests)} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
