#!/usr/bin/env python3
"""Opt-in agy credential seeding for newly created owner users."""

from __future__ import annotations

import os
import stat as stat_module

from team_launcher_test_helpers import *

SEED_SOURCE_USER = "seed-user"
SEED_TOKEN_TEXT = '{"auth_method":"consumer","token":{"refresh_token":"test-refresh-secret"}}\n'
TOKEN_NAME = "antigravity-oauth-token"


class _SeedingRunner(FakeRunner):
    def __init__(
        self,
        *,
        owner_user: str,
        project_dir: Path,
        sandbox_root: Path,
        owner_exists: bool = False,
        failing_install_fragment: str = "",
        failing_chown_fragment: str = "",
    ) -> None:
        super().__init__()
        self.owner_user = owner_user
        self.project_dir = project_dir
        self.sandbox_root = sandbox_root
        self.owner_exists = owner_exists
        self.failing_install_fragment = failing_install_fragment
        self.failing_chown_fragment = failing_chown_fragment

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:4] == ["sudo", "-u", self.owner_user, "git"]:
            git_args = args[3:]
            if len(git_args) >= 3 and git_args[:2] == ["git", "-C"] and Path(git_args[2]) == self.project_dir:
                return self._run_project_git(git_args, **kwargs)
            return subprocess.CompletedProcess(args, 0)
        if args[:4] == ["sudo", "-u", self.owner_user, "mkdir"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.project_dir:
            return self._run_project_git(args, **kwargs)
        if args[:2] == ["id", "-u"]:
            return subprocess.CompletedProcess(args, 0 if self.owner_exists else 1)
        if args[:2] == ["loginctl", "show-user"]:
            return subprocess.CompletedProcess(args, 0, stdout="yes\n")
        if args[:1] == ["useradd"]:
            return subprocess.CompletedProcess(args, 0)
        if args[:1] == ["chown"]:
            if self.failing_chown_fragment and self.failing_chown_fragment in " ".join(args):
                return subprocess.CompletedProcess(args, 1)
            return subprocess.CompletedProcess(args, 0)
        if args[:1] == ["install"]:
            return self._run_install(args)
        return super().__call__(args, **kwargs)

    def _in_sandbox(self, path: Path) -> bool:
        return path == self.sandbox_root or self.sandbox_root in path.parents

    def _run_install(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if self.failing_install_fragment and self.failing_install_fragment in " ".join(args):
            return subprocess.CompletedProcess(args, 1)
        # Only mirror on-disk effects inside the temp tree; the launcher also installs
        # absolute state dirs under the real /home, which this test must not touch.
        target = Path(args[-1])
        if "-d" in args and self._in_sandbox(target):
            target.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0)

    def _run_project_git(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        run_kwargs = dict(kwargs)
        run_kwargs.setdefault("text", True)
        run_kwargs.setdefault("stdout", subprocess.PIPE)
        run_kwargs.setdefault("stderr", subprocess.PIPE)
        return subprocess.run(args, **run_kwargs)


def _source_token_path(home_base: Path, *, user: str = SEED_SOURCE_USER) -> Path:
    return home_base / user / ".gemini" / "antigravity-cli" / TOKEN_NAME


def _seeded_token_path(home_base: Path) -> Path:
    return home_base / "otto-agent" / ".gemini" / "antigravity-cli" / TOKEN_NAME


def _seed_source_token(
    home_base: Path, *, user: str = SEED_SOURCE_USER, text: str = SEED_TOKEN_TEXT
) -> Path:
    token = _source_token_path(home_base, user=user)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(text, encoding="utf-8")
    return token


def _provision(
    tmp_path: Path,
    *,
    agy_credential_source: str | None,
    owner_exists: bool = False,
    failing_install_fragment: str = "",
    failing_chown_fragment: str = "",
) -> tuple[object, _SeedingRunner, str, Path, Path]:
    """Run a fresh noninteractive provision; returns (result_or_exit, runner, stdout, home_base, project_dir)."""
    home_base = tmp_path / "home"
    source_repo = tmp_path / "source-repo"
    source_repo.mkdir()
    _write_source_onboarding_docs(source_repo)
    project_dir = home_base / "otto-agent" / "Projects" / "porter_system"
    runner = _SeedingRunner(
        owner_user="otto-agent",
        project_dir=project_dir,
        sandbox_root=tmp_path,
        owner_exists=owner_exists,
        failing_install_fragment=failing_install_fragment,
        failing_chown_fragment=failing_chown_fragment,
    )
    stdout = StringIO()
    outcome: object
    try:
        with redirect_stdout(stdout):
            outcome = switchyard_new_command(
                desktop_policy=Path("headless"),
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter System",
                source_repo=source_repo,
                commit_git_dir="/srv/git/review-cache.git",
                role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                yes=True,
                allow_existing_owner_user=True,
                agy_credential_source=agy_credential_source,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                pane_state_dir=tmp_path / "pane-state",
                input_func=lambda _prompt: "",
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                session_record_timeout=0,
                registry_dir=tmp_path / "registry",
                layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                konsole_process_launcher=RecordingProcessLauncher(),
            )
    except SystemExit as exc:
        outcome = exc
    return outcome, runner, stdout.getvalue(), home_base, project_dir


def _token_chown_calls(runner: _SeedingRunner) -> list[list[str]]:
    return [call for call in runner.calls if call[:1] == ["chown"] and call[-1].endswith(TOKEN_NAME)]


def _mutated(runner: _SeedingRunner) -> list[list[str]]:
    """Provisioning calls that change the machine, in the order they were attempted."""
    return [
        call
        for call in runner.calls
        if call[:1] in (["useradd"], ["install"], ["chown"], ["loginctl"])
    ]


def test_fresh_provision_seeds_agy_token_into_created_owner_home() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        outcome, runner, output, home_base, project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER
        )
        seeded = _seeded_token_path(home_base)
        seeded_text = seeded.read_text(encoding="utf-8") if seeded.exists() else ""
        seeded_mode = stat_module.S_IMODE(seeded.stat().st_mode) if seeded.exists() else None
        artifact = json.loads(
            (project_dir / ".switchyard" / "porter.project.json").read_text(encoding="utf-8")
        )
        chown_calls = _token_chown_calls(runner)

    assert outcome == 0
    assert seeded_text == SEED_TOKEN_TEXT
    assert seeded_mode == 0o600
    assert chown_calls == [["chown", "otto-agent:otto-agent", str(_seeded_token_path(home_base))]]
    # The private directory is created owner-only before the token lands in it.
    assert any(
        c[:1] == ["install"] and "-d" in c and "0700" in c and c[-1].endswith(".gemini/antigravity-cli")
        for c in runner.calls
    )
    assert artifact["project"]["capability_grants"]["agy_credential_source"] == SEED_SOURCE_USER
    assert "seeded agy credential for otto-agent from seed-user" in output
    # The shared-account blast radius is stated where the operator will see it.
    assert "every role pane of this project shares that one account" in output
    # The token's contents never reach stdout.
    assert "test-refresh-secret" not in output


def test_provision_without_opt_in_does_not_seed_or_record_a_source() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        outcome, runner, output, home_base, project_dir = _provision(tmp_path, agy_credential_source=None)
        seeded_exists = _seeded_token_path(home_base).exists()
        artifact = json.loads(
            (project_dir / ".switchyard" / "porter.project.json").read_text(encoding="utf-8")
        )

    assert outcome == 0
    assert not seeded_exists
    assert not _token_chown_calls(runner)
    assert artifact["project"]["capability_grants"]["agy_credential_source"] == ""
    assert "seeded agy credential" not in output


def test_missing_source_is_rejected_before_any_provisioning_mutation() -> None:
    """A bad source must not leave a half-provisioned project that cannot be retried.

    Seeding refuses to touch an existing owner user, so if provisioning created the owner
    before noticing the bad source, the retry could never reach the requested end state.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        # Deliberately do not create the source token.
        outcome, runner, _output, home_base, project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER
        )
        seeded_exists = _seeded_token_path(home_base).exists()
        project_dir_exists = project_dir.exists()
        mutations = _mutated(runner)

    assert isinstance(outcome, SystemExit)
    message = str(outcome)
    assert "agy credential seeding was requested from 'seed-user'" in message
    assert f"sign in to agy as {SEED_SOURCE_USER} first" in message
    assert "clear capability_grants.agy_credential_source" in message
    assert not seeded_exists
    assert not project_dir_exists
    assert mutations == [], f"provisioning mutated the machine before validating: {mutations}"


def test_symlinked_source_token_is_refused() -> None:
    """The source home is unprivileged, so a link out of it must not be followed."""
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        secret = tmp_path / "root-only-secret"
        secret.write_text("root-readable-secret\n", encoding="utf-8")
        token = _source_token_path(home_base)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.symlink_to(secret)
        outcome, runner, _output, home_base, _project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER
        )
        seeded_exists = _seeded_token_path(home_base).exists()
        mutations = _mutated(runner)

    assert isinstance(outcome, SystemExit)
    assert "is a symlink" in str(outcome)
    assert "will not follow a link out of it" in str(outcome)
    assert not seeded_exists
    assert mutations == []


def test_non_regular_source_token_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        token = _source_token_path(tmp_path / "home")
        token.mkdir(parents=True, exist_ok=True)
        outcome, runner, _output, _home_base, _project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER
        )

    assert isinstance(outcome, SystemExit)
    assert "is not a regular file" in str(outcome)
    assert _mutated(runner) == []


def test_source_token_not_owned_by_source_user_is_refused() -> None:
    """Uses 'root' as the source: a real passwd entry whose uid the test file cannot match."""
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home", user="root")
        outcome, runner, _output, _home_base, _project_dir = _provision(
            tmp_path, agy_credential_source="root"
        )

    assert isinstance(outcome, SystemExit)
    message = str(outcome)
    assert "is owned by uid" in message
    assert "refusing to copy a token that user does not own" in message
    assert _mutated(runner) == []


def test_failed_ownership_assignment_aborts_provisioning() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        outcome, _runner, _output, _home_base, _project_dir = _provision(
            tmp_path,
            agy_credential_source=SEED_SOURCE_USER,
            failing_chown_fragment=TOKEN_NAME,
        )

    assert isinstance(outcome, SystemExit)
    assert "failed to assign" in str(outcome)


def test_failed_private_directory_creation_aborts_before_copying() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        outcome, _runner, _output, home_base, _project_dir = _provision(
            tmp_path,
            agy_credential_source=SEED_SOURCE_USER,
            failing_install_fragment="0700",
        )
        seeded_exists = _seeded_token_path(home_base).exists()

    assert isinstance(outcome, SystemExit)
    assert "failed to create" in str(outcome)
    assert not seeded_exists


def test_existing_owner_user_is_never_modified() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        _outcome, runner, output, home_base, _project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER, owner_exists=True
        )
        seeded_exists = _seeded_token_path(home_base).exists()

    assert not seeded_exists
    assert not _token_chown_calls(runner)
    assert "not seeding agy credential into existing user otto-agent" in output


def test_source_user_must_be_a_plain_user_name() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        outcome, runner, _output, _home_base, _project_dir = _provision(
            tmp_path, agy_credential_source="../../etc"
        )

    assert isinstance(outcome, SystemExit)
    assert "must be a plain Unix user name" in str(outcome)
    assert _mutated(runner) == []


def test_artifact_rejects_traversal_in_agy_credential_source() -> None:
    try:
        team_launcher._artifact_capability_grants(
            {"agy_credential_source": "../../etc"}, path=Path("artifact.json")
        )
        raise AssertionError("expected traversal rejection")
    except SystemExit as exc:
        message = str(exc)

    assert "capability_grants.agy_credential_source" in message
    assert "must be a plain Unix user name" in message


def test_artifact_rejects_non_string_agy_credential_source() -> None:
    try:
        team_launcher._artifact_capability_grants({"agy_credential_source": 7}, path=Path("artifact.json"))
        raise AssertionError("expected type rejection")
    except SystemExit as exc:
        message = str(exc)

    assert "capability_grants.agy_credential_source" in message
    assert "must be a JSON string" in message


def test_artifact_defaults_agy_credential_source_to_disabled() -> None:
    grants = team_launcher._artifact_capability_grants(None, path=Path("artifact.json"))
    assert grants["agy_credential_source"] == ""


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_agy_credential_seeding_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
