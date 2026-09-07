#!/usr/bin/env python3
"""SYRD-61: a pinned release must survive the operator handoff, and a rollback
must report what actually came back.

The rollout behind this pinned an installed release on the outer command --
`--source-repo /opt/switchyard/current --commit-git-dir <cache> --deploy-ref
<sha>` -- and the upgrade stopped at the accounts phase, which asks an operator
to run a generated script. That script ends by handing the upgrade back with
`sudo switchyard upgrade <project>`: no arguments, and `sudo` scrubs the
environment, so by the time the identities transaction needed a release there
was nothing left to name one. It refused to guess, rolled back, and recorded
three more untruths on the way out -- that the notification listener could not
be started, and that all six roles "did not come back" while six live sessions
said otherwise.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from team_launcher_test_helpers import *
from team_launcher_upgrade_cutover_test import (
    _RunningTenant,
    _board_with_marker,
    _declarative_tenant,
    _deployed_release,
    _live_runner,
    _upgrade,
)

PROJECT = "porter"
ROLE_ACCOUNTS = {f"{PROJECT}-{role}" for role in ("designer", "director", "audit", "ops", "app", "main")}
SCRUBBED = ("SWITCHYARD_BARE_REPO",)


class _Environment:
    """Save and restore exactly the variables a case moves."""

    KEYS = (
        "SWITCHYARD_BARE_REPO",
        "SWITCHYARD_SHARED_INSTALL_ROOT",
        "SWITCHYARD_PRIVILEGED_PROVISION_ROOT",
    )

    def __enter__(self):
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        return self

    def __exit__(self, *_exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _installed_release(tmp: Path, sha: str) -> Path:
    """An installed shared release: a marker, no history, nothing to resolve from."""
    shared_root = tmp / "opt" / "switchyard"
    release = shared_root / "releases" / sha
    release.mkdir(parents=True)
    (release / ".switchyard-release.json").write_text(
        json.dumps({"commit": sha}) + "\n", encoding="utf-8"
    )
    current = shared_root / "current"
    current.symlink_to(release)
    os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(shared_root)
    return current


def _resolved(path: Path) -> str:
    """What the record holds: the tree, not the name that points at it."""
    return str(path.resolve())


def _pinned_tenant(tmp: Path) -> tuple[Path, Path, Path, str, Path]:
    """A legacy tenant, an installed release to deploy, and the cache holding it."""
    cache, _checkout = _make_origin_backed_repo(tmp)
    sha = _run_git(
        ["git", f"--git-dir={cache}", "rev-parse", "--verify", "refs/heads/main"]
    ).stdout.strip()
    release = _installed_release(tmp, sha)
    board_root = _deployed_release(tmp, PROJECT, "1" * 40)
    config_path, _ = _declarative_tenant(tmp, board_root=board_root)
    return config_path, release, cache, sha, board_root


def _pinned_runner(inner, *, board_root: Path, sha: str, seen: list[list[str]], deploys: list[str]):
    """The host, with the two things a pinned release depends on made real.

    Reading the commit out of the named cache is a real `git` read of a real
    repository, and the deploy really moves `current`. Everything either one
    touches is recorded, so a case can prove the resolution reached the cache
    that was pinned and no network at all.
    """

    def call(args, **kwargs):
        argv = [str(part) for part in args]
        seen.append(argv)
        bare = argv
        if bare[:2] == ["sudo", "-u"] and len(bare) > 3:
            bare = bare[4:] if bare[3] == "-H" else bare[3:]
        if bare[:1] == ["git"]:
            passthrough = dict(kwargs)
            passthrough.setdefault("text", True)
            passthrough.setdefault("stdout", subprocess.PIPE)
            passthrough.setdefault("stderr", subprocess.PIPE)
            return subprocess.run(bare, **passthrough)
        if "deploy-restart" in " ".join(argv):
            deploys.append(" ".join(argv))
            release = board_root / "releases" / sha
            release.mkdir(parents=True, exist_ok=True)
            (release / ".pgu-deploy-sha").write_text(sha + "\n", encoding="utf-8")
            current = board_root / "current"
            if current.is_symlink() or current.exists():
                current.unlink()
            current.symlink_to(release)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return inner(argv, **kwargs)

    return call


def _run_upgrade(config_path: Path, *, exists, runner, repo_root: Path, **selection):
    """The privileged upgrade, with the source selection stated exactly as a CLI would.

    `repo_root` is where the running launcher lives. On a host that is
    `/opt/switchyard/current`, an installed release with no history of its own,
    and it is the thing an unpinned upgrade falls back to. Pinning it here keeps
    these cases off whatever checkout happens to be running them.
    """
    printed: list[str] = []
    original_euid = team_launcher.os.geteuid
    original_exists = team_launcher.local_account_exists
    original_migrate = team_launcher.migrate_declarative_director_onboarding
    original_opener = team_launcher._open_board_url
    original_repo_root = team_launcher._repo_root
    try:
        team_launcher._repo_root = lambda: repo_root
        team_launcher._open_board_url = _board_with_marker(False)
        team_launcher.os.geteuid = lambda: 0
        team_launcher.local_account_exists = lambda account: account in exists
        team_launcher.migrate_declarative_director_onboarding = lambda config, **kwargs: True
        config = team_launcher.load_project_config(PROJECT, config_path)
        result = team_launcher.upgrade_project_command(
            config, config_path=config_path, runner=runner, print_func=printed.append, **selection
        )
    finally:
        team_launcher.os.geteuid = original_euid
        team_launcher.local_account_exists = original_exists
        team_launcher.migrate_declarative_director_onboarding = original_migrate
        team_launcher._open_board_url = original_opener
        team_launcher._repo_root = original_repo_root
    return result, "\n".join(printed)


def _handoff_arguments(config_path: Path) -> dict[str, object]:
    """Parse the generated script's own continuation with the real CLI parser.

    Not a re-description of it: the line the operator runs is taken out of the
    artifact and handed to the parser `switchyard upgrade` actually uses, so
    whatever that line does or does not carry is what this drives.
    """
    script = (config_path.with_name(f"{PROJECT}-role-accounts.sh")).read_text(encoding="utf-8")
    lines = [
        line for line in script.splitlines()
        if line.startswith("sudo switchyard upgrade ")
    ]
    assert len(lines) == 1, lines
    argv = shlex.split(lines[0])
    assert argv[:3] == ["sudo", "switchyard", "upgrade"], argv
    parsed = team_launcher._build_switchyard_upgrade_parser().parse_args(argv[3:])
    assert parsed.project == PROJECT, parsed
    return {
        "source_repo": parsed.source_repo,
        "commit_git_dir": parsed.commit_git_dir,
        "deploy_ref": parsed.deploy_ref,
    }


def _identities_phase(config_path: Path) -> dict[str, object]:
    config = team_launcher.load_project_config(PROJECT, config_path)
    journal = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
    return journal["phases"].get("identities", {})


def _scrub() -> None:
    for key in SCRUBBED:
        os.environ.pop(key, None)


def test_the_pinned_release_survives_the_operator_handoff() -> None:
    """The whole incident, from the outer pin to the transaction that needs it."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-handoff.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, board_root = _pinned_tenant(tmp)
        seen: list[list[str]] = []
        deploys: list[str] = []

        # The outer command an operator ran: an installed release, the cache
        # that holds its history, and the exact commit to deploy.
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _run_upgrade(
                config_path,
                exists=set(),
                repo_root=release,
                runner=_pinned_runner(
                    tenant.runner(), board_root=board_root, sha=sha, seen=seen, deploys=deploys
                ),
                source_repo=release,
                commit_git_dir=str(cache),
                deploy_ref=sha,
            )
        assert result == 0, output
        assert "role-accounts.sh" in output, output

        # The operator runs the generated script. Its continuation names the
        # release this upgrade was pinned to, so the operator can read it.
        script = config_path.with_name(f"{PROJECT}-role-accounts.sh").read_text(encoding="utf-8")
        assert _resolved(release) in script, script[-2000:]
        assert str(cache) in script, script[-2000:]
        assert sha in script, script[-2000:]

        # And then the handoff itself: no arguments, and no environment.
        _scrub()
        assert "SWITCHYARD_BARE_REPO" not in os.environ
        seen.clear()
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _run_upgrade(
                config_path,
                exists=ROLE_ACCOUNTS,
                repo_root=release,
                runner=_pinned_runner(
                    tenant.runner(), board_root=board_root, sha=sha, seen=seen, deploys=deploys
                ),
                **_handoff_arguments(config_path),
            )
        assert result == 0, output
        assert "keeps the release this upgrade was pinned to" in output, output
        # It reached identity cutover, and it deployed the pinned commit out of
        # the pinned cache.
        phase = _identities_phase(config_path)
        assert phase.get("state") == "done", (phase, output)
        assert deploys, output
        assert sha in deploys[-1], deploys[-1]
        assert str(cache) in deploys[-1], deploys[-1]
        # Nothing was guessed and nothing was fetched: the commit came out of a
        # local repository somebody named.
        assert any(
            argv[:1] == ["git"] and f"--git-dir={cache}" in argv for argv in seen
        ), seen[:20]
        for argv in seen:
            joined = " ".join(argv)
            assert "ls-remote" not in joined, joined
            assert " fetch" not in f" {joined}", joined


def test_a_handoff_with_nothing_recorded_still_refuses_to_guess() -> None:
    """Fail closed, exactly as the incident did, rather than deploy a guess."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-missing.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, board_root = _pinned_tenant(tmp)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _run_upgrade(
                config_path,
                exists=set(),
                repo_root=release,
                runner=_pinned_runner(
                    tenant.runner(), board_root=board_root, sha=sha, seen=[], deploys=[]
                ),
                source_repo=release,
                commit_git_dir=str(cache),
                deploy_ref=sha,
            )
        config = team_launcher.load_project_config(PROJECT, config_path)
        team_launcher.privileged_upgrade_source_path(config).unlink()
        _scrub()
        deploys: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _run_upgrade(
                config_path,
                exists=ROLE_ACCOUNTS,
                repo_root=release,
                runner=_pinned_runner(
                    tenant.runner(), board_root=board_root, sha=sha, seen=[], deploys=deploys
                ),
                **_handoff_arguments(config_path),
            )
        # The upgrade itself still reports every phase, as it does whenever it
        # stops at one root does not own; what must not happen is a deploy.
        assert result == 0, output
        assert deploys == [], deploys
        assert "rolling porter back" in output, output
        phase = _identities_phase(config_path)
        assert phase.get("state") == "rolled back", phase
        assert "the release to deploy could not be resolved" in phase.get("detail", ""), phase
        # The refusal names both ways to supply a cache, so an operator reading
        # it after a `sudo` handoff is not sent back to an environment variable
        # the handoff scrubbed.
        assert "--commit-git-dir" in phase.get("detail", ""), phase


def test_a_record_the_tenant_could_have_written_is_not_a_pinned_release() -> None:
    """It decides what root deploys, so anyone but root writing it voids it."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-tamper.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, board_root = _pinned_tenant(tmp)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _run_upgrade(
                config_path,
                exists=set(),
                repo_root=release,
                runner=_pinned_runner(
                    tenant.runner(), board_root=board_root, sha=sha, seen=[], deploys=[]
                ),
                source_repo=release,
                commit_git_dir=str(cache),
                deploy_ref=sha,
            )
        config = team_launcher.load_project_config(PROJECT, config_path)
        record = team_launcher.privileged_upgrade_source_path(config)
        assert team_launcher.read_upgrade_source(config)["deploy_ref"] == sha
        record.chmod(0o664)
        assert team_launcher.read_upgrade_source(config) == {}, record.read_text()
        record.chmod(0o644)
        assert team_launcher.read_upgrade_source(config)["deploy_ref"] == sha
        # A record for some other project is not this project's record either.
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["project"] = "somebody-else"
        record.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        assert team_launcher.read_upgrade_source(config) == {}


def test_an_explicit_selection_still_wins_over_the_record() -> None:
    """The record fills gaps; it never overrides an operator who said otherwise.

    Including an operator who says `--deploy-ref origin/main`. That is a
    deliberate reset back to the branch, and it has to be distinguishable from
    saying nothing at all -- a default-valued argument that reads as omission
    hands them back the very commit they are trying to leave.
    """
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-override.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, _board_root = _pinned_tenant(tmp)
        config = team_launcher.load_project_config(PROJECT, config_path)
        original_euid = team_launcher.os.geteuid
        try:
            team_launcher.os.geteuid = lambda: 0
            assert not team_launcher.record_upgrade_source(
                config, source_repo=release, commit_git_dir=str(cache), deploy_ref=sha
            )
        finally:
            team_launcher.os.geteuid = original_euid

        elsewhere = tmp / "another-checkout"
        chosen_repo, chosen_cache, chosen_ref, used = team_launcher.resolve_pinned_upgrade_source(
            config, source_repo=elsewhere, commit_git_dir=None, deploy_ref=None
        )
        assert chosen_repo == elsewhere
        assert chosen_cache == str(cache)
        assert chosen_ref == sha
        assert "source" not in used, used

        # Said nothing: all three come back.
        chosen_repo, chosen_cache, chosen_ref, used = team_launcher.resolve_pinned_upgrade_source(
            config, source_repo=None, commit_git_dir=None, deploy_ref=None
        )
        assert (str(chosen_repo), chosen_cache, chosen_ref) == (_resolved(release), str(cache), sha)
        assert "source" in used and "commit cache" in used and "deploy ref" in used, used

        # Said "go back to the branch": that wins, and the source and cache are
        # still filled in from the record.
        chosen_repo, chosen_cache, chosen_ref, used = team_launcher.resolve_pinned_upgrade_source(
            config,
            source_repo=None,
            commit_git_dir=None,
            deploy_ref=team_launcher.DEFAULT_TENANT_RELEASE_DEPLOY_REF,
        )
        assert chosen_ref == team_launcher.DEFAULT_TENANT_RELEASE_DEPLOY_REF, chosen_ref
        assert str(chosen_repo) == _resolved(release), chosen_repo
        assert "deploy ref" not in used, used


def test_the_parser_tells_an_omitted_ref_from_a_default_valued_one() -> None:
    """The distinction has to survive parsing, or resolution never sees it."""
    for parser in (
        team_launcher._build_switchyard_upgrade_parser(),
        team_launcher._build_switchyard_finish_upgrade_parser(),
    ):
        assert parser.parse_args([PROJECT]).deploy_ref is None
        explicit = parser.parse_args(
            [PROJECT, "--deploy-ref", team_launcher.DEFAULT_TENANT_RELEASE_DEPLOY_REF]
        )
        assert explicit.deploy_ref == team_launcher.DEFAULT_TENANT_RELEASE_DEPLOY_REF


def test_a_release_symlink_that_moves_does_not_move_the_pinned_release() -> None:
    """`/opt/switchyard/current` is a name, and installing a release moves it.

    Between the accounts phase and the rerun it asks for, `current` can come to
    mean a different tree. A record holding the name would pin nothing: the
    resumed phase would regenerate artifacts from the newer release while still
    calling the selection pinned.
    """
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-moved.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, board_root = _pinned_tenant(tmp)
        original_tree = release.resolve()
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _run_upgrade(
                config_path,
                exists=set(),
                repo_root=release,
                runner=_pinned_runner(
                    tenant.runner(), board_root=board_root, sha=sha, seen=[], deploys=[]
                ),
                source_repo=release,
                commit_git_dir=str(cache),
                deploy_ref=sha,
            )
        assert result == 0, output

        # A newer shared release is installed, and `current` follows it.
        newer_sha = "b" * 40
        newer = original_tree.parent / newer_sha
        newer.mkdir()
        (newer / ".switchyard-release.json").write_text(
            json.dumps({"commit": newer_sha}) + "\n", encoding="utf-8"
        )
        release.unlink()
        release.symlink_to(newer)
        assert release.resolve() == newer

        _scrub()
        deploys: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _run_upgrade(
                config_path,
                exists=ROLE_ACCOUNTS,
                repo_root=release,
                runner=_pinned_runner(
                    tenant.runner(), board_root=board_root, sha=sha, seen=[], deploys=deploys
                ),
                **_handoff_arguments(config_path),
            )
        assert result == 0, output
        # The release the operator was looking at, not the one `current` means now.
        assert str(original_tree) in output, output
        assert newer_sha not in output, output
        assert deploys and sha in deploys[-1], deploys
        assert newer_sha not in deploys[-1], deploys[-1]
        config = team_launcher.load_project_config(PROJECT, config_path)
        assert team_launcher.read_upgrade_source(config)["source_repo"] == str(original_tree)


def _no_handoff_was_advertised(config_path: Path) -> None:
    """Nothing to run, and no phase recorded as somewhere to resume from."""
    assert not config_path.with_name(f"{PROJECT}-role-accounts.sh").exists()
    assert not config_path.with_name(f"{PROJECT}-upgrade.json").exists()


def test_a_pin_that_cannot_be_recorded_refuses_before_advertising_a_handoff() -> None:
    """Accepting it would schedule this incident for the next phase."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-unwritable.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, board_root = _pinned_tenant(tmp)
        _no_handoff_was_advertised(config_path)
        # Root's own directory cannot be created: something else is in its way.
        blocked = tmp / "blocked-privileged-root"
        blocked.write_text("not a directory\n", encoding="utf-8")
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(blocked)

        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output = _run_upgrade(
                config_path,
                exists=set(),
                repo_root=release,
                runner=_pinned_runner(
                    tenant.runner(), board_root=board_root, sha=sha, seen=[], deploys=[]
                ),
                source_repo=release,
                commit_git_dir=str(cache),
                deploy_ref=sha,
            )
        assert result == 1, output
        assert "could not record the pinned release" in output, output
        assert "refusing to upgrade" in output, output
        assert "Nothing was changed" in output, output
        _no_handoff_was_advertised(config_path)


def test_a_record_that_does_not_read_back_refuses_the_same_way() -> None:
    """A write that returned success is not a record; being able to read it is."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-unreadable.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, board_root = _pinned_tenant(tmp)
        original_write = team_launcher._write_privileged_json
        try:
            # A filesystem that accepts the write and does not keep it.
            team_launcher._write_privileged_json = lambda _path, _payload: ""
            with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                result, output = _run_upgrade(
                    config_path,
                    exists=set(),
                    repo_root=release,
                    runner=_pinned_runner(
                        tenant.runner(), board_root=board_root, sha=sha, seen=[], deploys=[]
                    ),
                    source_repo=release,
                    commit_git_dir=str(cache),
                    deploy_ref=sha,
                )
        finally:
            team_launcher._write_privileged_json = original_write
        assert result == 1, output
        assert "did not read back as it was written" in output, output
        _no_handoff_was_advertised(config_path)


def test_an_unprivileged_upgrade_says_the_pin_was_not_recorded() -> None:
    """It runs no phase that resolves a release, and it promises none."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-unprivileged.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, board_root = _pinned_tenant(tmp)
        printed: list[str] = []
        original_repo_root = team_launcher._repo_root
        original_opener = team_launcher._open_board_url
        try:
            team_launcher._repo_root = lambda: release
            team_launcher._open_board_url = _board_with_marker(False)
            config = team_launcher.load_project_config(PROJECT, config_path)
            result = team_launcher.upgrade_project_command(
                config,
                config_path=config_path,
                source_repo=release,
                commit_git_dir=str(cache),
                deploy_ref=sha,
                runner=_pinned_runner(
                    _live_runner(config_path), board_root=board_root, sha=sha, seen=[], deploys=[]
                ),
                print_func=printed.append,
            )
        finally:
            team_launcher._repo_root = original_repo_root
            team_launcher._open_board_url = original_opener
        output = "\n".join(printed)
        assert result == 0, output
        assert "is not root, so porter's pinned release is not recorded" in output, output
        assert team_launcher.read_upgrade_source(config) == {}
        # And the continuation it wrote does not pretend otherwise.
        script = config_path.with_name(f"{PROJECT}-role-accounts.sh").read_text(encoding="utf-8")
        assert "No pinned release was recorded for this upgrade" in script, script[-1500:]


def _systemctl_probe(tmp: Path) -> tuple[Path, Path]:
    """A `systemctl` that records the environment it was actually given.

    It fails without a runtime directory, because that is what the real one
    does: with no `XDG_RUNTIME_DIR` and no bus address there is no user manager
    to talk to, and `systemctl --user` exits 1.
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp / "systemctl.log"
    stub = bin_dir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s|%s\\n" "$*" "${{XDG_RUNTIME_DIR:-}}" "${{DBUS_SESSION_BUS_ADDRESS:-}}" '
        f">> {shlex.quote(str(log))}\n"
        'if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then\n'
        '    echo "Failed to connect to bus: No medium found" >&2\n'
        "    exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir, log


def _run_owner_script(argv: list[str], bin_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the script the launcher built, with nothing else on the path."""
    script = argv[argv.index("-c") + 1]
    return subprocess.run(
        ["sh", "-c", script],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(bin_dir.parent)},
        text=True,
        capture_output=True,
    )


def test_every_command_in_the_owner_script_gets_the_owner_runtime() -> None:
    """A prefix binds to one command; `restart` is the second of two."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="owner-runtime.") as raw:
        tmp = Path(raw)
        config_path, _ = _declarative_tenant(tmp)
        config = team_launcher.load_project_config(PROJECT, config_path)
        bin_dir, log = _systemctl_probe(tmp)
        unit = team_launcher._listener_user_unit(config)

        for action in ("restart", "start", "stop", "is-active"):
            log.unlink(missing_ok=True)
            argv = team_launcher._owner_user_systemctl(
                config, action, unit, config_path=config_path
            )
            result = _run_owner_script(argv, bin_dir)
            assert result.returncode == 0, (action, result.stdout, result.stderr)
            calls = [line.split("|") for line in log.read_text(encoding="utf-8").splitlines()]
            assert calls, action
            assert any(call[0].startswith(f"--user {action}") for call in calls), (action, calls)
            for invocation, runtime, bus in calls:
                assert runtime, (action, invocation)
                assert bus.startswith("unix:path="), (action, invocation, bus)


def test_the_printed_listener_instruction_carries_the_runtime_too() -> None:
    """An operator pastes this one; it must not half-work either."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="listener-instruction.") as raw:
        tmp = Path(raw)
        board_root = _deployed_release(tmp, PROJECT, "1" * 40)
        config_path, _ = _declarative_tenant(tmp, board_root=board_root)
        config = team_launcher.load_project_config(PROJECT, config_path)
        status = team_launcher.tenant_release_status(config, config_path=config_path)
        assert status is not None
        bin_dir, log = _systemctl_probe(tmp)
        for action in ("start", "stop"):
            log.unlink(missing_ok=True)
            rendered = team_launcher.tenant_release_listener_command(status, PROJECT, action)
            result = _run_owner_script(shlex.split(rendered), bin_dir)
            assert result.returncode == 0, (action, result.stderr)
            for line in log.read_text(encoding="utf-8").splitlines():
                _invocation, runtime, bus = line.split("|")
                assert runtime and bus.startswith("unix:path="), line


def test_a_listener_that_starts_and_dies_is_not_reported_restored() -> None:
    """A zero exit from `restart` is a request accepted, not a unit running."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="listener-dies.") as raw:
        tmp = Path(raw)
        config_path, _ = _declarative_tenant(tmp)
        config = team_launcher.load_project_config(PROJECT, config_path)

        def accepts_then_dies(args, **_kwargs):
            joined = " ".join(str(part) for part in args)
            if "is-active" in joined:
                return subprocess.CompletedProcess(list(args), 3, "failed\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        problems = team_launcher.start_owner_listener(
            config, runner=accepts_then_dies, config_path=config_path
        )
        assert problems, problems
        assert "was started but is failed" in problems[0], problems


def _rolls_back(config_path: Path, *, runner, launcher=None):
    """Drive the transaction to a rollback and return what it printed."""
    printed: list[str] = []
    # The transaction is root's phase, and root's own journal is the copy every
    # later gate reads.
    original_euid = team_launcher.os.geteuid
    try:
        team_launcher.os.geteuid = lambda: 0
        result = team_launcher.cutover_role_identities_command(
            team_launcher.load_project_config(PROJECT, config_path),
            config_path=config_path,
            runner=runner,
            launcher=launcher,
            print_func=printed.append,
        )
    finally:
        team_launcher.os.geteuid = original_euid
    return result, "\n".join(printed)


def test_the_rollback_brings_the_listener_back_after_the_roles() -> None:
    """The units go back before the workers do, so it is checked after them.

    A listener restored and then lost while the sessions come back is a listener
    the tenant does not have, and a rollback that only checked at the moment it
    put the unit file back would report one it had.
    """
    with _Environment(), tempfile.TemporaryDirectory(prefix="rollback-listener.") as raw:
        tmp = Path(raw)
        config_path, _ = _declarative_tenant(tmp)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            tenant.listener_active = True
            starts: list[int] = []

            def launcher(config, **_kwargs):
                starts.append(1)
                if len(starts) == 1:
                    # The forward pass fails here, so the transaction unwinds
                    # with the listener untouched.
                    return 1
                # The rollback's restart, which takes the listener down with it.
                tenant.live = True
                tenant.listener_active = False
                return 0

            result, output = _rolls_back(
                config_path, runner=tenant.runner(), launcher=launcher
            )
        assert result == 1, output
        assert len(starts) == 2, starts
        # Restored, lost, and brought back: the tenant ends with a listener.
        assert tenant.listener_active is True, tenant.listener_calls
        assert "role session(s) are live" in output, output


def test_a_restart_that_was_never_attempted_is_not_a_role_that_did_not_come_back() -> None:
    """And the record says what is live once the rollback has finished."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="rollback-truth.") as raw:
        tmp = Path(raw)
        board_root = _deployed_release(tmp, PROJECT, "1" * 40)
        config_path, _ = _declarative_tenant(tmp, board_root=board_root)
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            inner = tenant.runner()

            def unresolvable_release(args, **kwargs):
                argv = [str(part) for part in args]
                bare = argv[4:] if argv[:2] == ["sudo", "-u"] and len(argv) > 4 else argv
                if bare[:1] == ["git"] and "rev-parse" in bare:
                    # The incident: an installed release and no cache to read.
                    return subprocess.CompletedProcess(argv, 128, "", "not a git repository")
                return inner(argv, **kwargs)

            result, output = _rolls_back(config_path, runner=unresolvable_release)
        assert result == 1, output
        assert "the release to deploy could not be resolved" in output, output
        # The roles were never asked to come back, so nothing may claim they failed to.
        assert "did not come back" not in output.split("after the rollback")[0], output
        assert "role session(s) are live" in output, output
        phase = _identities_phase(config_path)
        detail = phase.get("detail", "")
        assert phase.get("state") == "rolled back", phase
        assert f"after the rollback {len(roles)} of {len(roles)} role session(s) are live" in detail, detail
        for role in roles:
            assert f"{role} did not come back" not in detail, detail


def test_the_resume_is_idempotent_from_the_exact_partial_state() -> None:
    """Accounts made, no per-role tmux server, configuration back on the owner."""
    with _Environment(), tempfile.TemporaryDirectory(prefix="pinned-resume.") as raw:
        tmp = Path(raw)
        config_path, release, cache, sha, board_root = _pinned_tenant(tmp)
        config = team_launcher.load_project_config(PROJECT, config_path)
        original_euid = team_launcher.os.geteuid
        try:
            team_launcher.os.geteuid = lambda: 0
            team_launcher.record_upgrade_source(
                config, source_repo=release, commit_git_dir=str(cache), deploy_ref=sha
            )
        finally:
            team_launcher.os.geteuid = original_euid
        _scrub()
        # Every role runs as the project account, and the configuration names it.
        assert all(
            not role.get("run_as_user")
            for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]
        )
        deploys: list[str] = []
        outputs: list[str] = []
        for _pass in range(2):
            with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                result, output = _run_upgrade(
                    config_path,
                    exists=ROLE_ACCOUNTS,
                    repo_root=release,
                    runner=_pinned_runner(
                        tenant.runner(), board_root=board_root, sha=sha, seen=[], deploys=deploys
                    ),
                )
            assert result == 0, output
            outputs.append(output)
        assert _identities_phase(config_path).get("state") == "done", _identities_phase(config_path)
        # The second pass had nothing left to move: the release was already the
        # pinned one, so it deployed once and not twice.
        assert len(deploys) == 1, deploys
        assert "part-way onto per-role accounts" not in outputs[1], outputs[1]
        assert "rolling" not in outputs[1], outputs[1]


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_pinned_release_resume_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
