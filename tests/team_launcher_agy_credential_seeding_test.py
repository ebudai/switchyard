#!/usr/bin/env python3
"""Opt-in agy credential seeding for newly created owner users."""

from __future__ import annotations

import os
import pwd
import re
import stat as stat_module
from contextlib import contextmanager

from team_launcher_test_helpers import *

# A real passwd-backed account that owns the fixture token it writes. Production
# validation requires both, so the fixture must satisfy them rather than rely on the
# check failing open for an unknown name.
SEED_SOURCE_USER = team_launcher.current_user_name()
ABSENT_SOURCE_USER = "syrd-no-such-source"
# The provisioned owner throughout this module. Real on this host, and deliberately not
# the test runner, so owner-uid checks are exercised rather than trivially satisfied.
OWNER_USER = "otto-agent"
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
        swap_credential_dir_to: Path | None = None,
    ) -> None:
        super().__init__()
        self.owner_user = owner_user
        self.project_dir = project_dir
        self.sandbox_root = sandbox_root
        self.owner_exists = owner_exists
        self.failing_install_fragment = failing_install_fragment
        self.failing_chown_fragment = failing_chown_fragment
        self.swap_credential_dir_to = swap_credential_dir_to

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
            # Simulate the owner winning the race: they control this directory, so they
            # can replace it with a symlink the instant install(1) returns.
            if self.swap_credential_dir_to is not None and target.name == "antigravity-cli":
                target.rmdir()
                target.symlink_to(self.swap_credential_dir_to, target_is_directory=True)
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


def _host_setting_path(tmp_path: Path) -> Path:
    return tmp_path / "etc" / "agy-credential.json"


@contextmanager
def _invoking_human(user: str):
    """Pin who switchyard considers the invoking human, so the proposal is deterministic."""
    previous = os.environ.get(team_launcher.GUI_USER_ENV)
    os.environ[team_launcher.GUI_USER_ENV] = user
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(team_launcher.GUI_USER_ENV, None)
        else:
            os.environ[team_launcher.GUI_USER_ENV] = previous


def _scripted_input(answers: dict[str, str] | None) -> Callable[[str], str]:
    """Answer prompts by matching their text, since prompt order is not this test's concern."""
    keyed = dict(answers or {})

    def _input(prompt: str) -> str:
        for fragment, reply in keyed.items():
            if fragment in prompt:
                return reply
        return ""

    return _input


def _provision(
    tmp_path: Path,
    *,
    agy_credential_source: str | None,
    owner_exists: bool = False,
    failing_install_fragment: str = "",
    failing_chown_fragment: str = "",
    allow_existing_owner_user: bool = True,
    swap_credential_dir_to: Path | None = None,
    no_agy_credential: bool = False,
    yes: bool = True,
    answers: dict[str, str] | None = None,
) -> tuple[object, _SeedingRunner, str, Path, Path]:
    """Run a provision; returns (result_or_exit, runner, stdout, home_base, project_dir).

    The host-setting path is always redirected under tmp_path so no test can read or
    write this machine's real /etc/switchyard/agy-credential.json.
    """
    home_base = tmp_path / "home"
    source_repo = tmp_path / "source-repo"
    source_repo.mkdir(exist_ok=True)
    _write_source_onboarding_docs(source_repo)
    project_dir = home_base / "otto-agent" / "Projects" / "porter_system"
    runner = _SeedingRunner(
        owner_user="otto-agent",
        project_dir=project_dir,
        sandbox_root=tmp_path,
        owner_exists=owner_exists,
        failing_install_fragment=failing_install_fragment,
        failing_chown_fragment=failing_chown_fragment,
        swap_credential_dir_to=swap_credential_dir_to,
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
                yes=yes,
                allow_existing_owner_user=allow_existing_owner_user,
                agy_credential_source=agy_credential_source,
                no_agy_credential=no_agy_credential,
                agy_credential_settings_path=_host_setting_path(tmp_path),
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                pane_state_dir=tmp_path / "pane-state",
                input_func=_scripted_input(answers),
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
    """Ownership is assigned through the open descriptor, so the path is /proc/<pid>/fd/N."""
    return [call for call in runner.calls if call[:1] == ["chown"] and "/fd/" in call[-1]]


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
    assert len(chown_calls) == 1
    assert chown_calls[0][:2] == ["chown", f"{OWNER_USER}:{OWNER_USER}"]
    assert re.fullmatch(r"/proc/\d+/fd/\d+", chown_calls[0][2]), chown_calls[0][2]
    # The private directory is created owner-only before the token lands in it.
    assert any(
        c[:1] == ["install"] and "-d" in c and "0700" in c and c[-1].endswith(".gemini/antigravity-cli")
        for c in runner.calls
    )
    assert artifact["project"]["capability_grants"]["agy_credential_source"] == SEED_SOURCE_USER
    assert f"seeded agy credential for otto-agent from {SEED_SOURCE_USER}" in output
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
    assert f"agy credential seeding was requested from {SEED_SOURCE_USER!r}" in message
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


def test_source_user_absent_from_passwd_is_refused() -> None:
    """Fails closed: with no uid to compare against there is no owner boundary at all.

    A deleted or mistyped account whose stale home survives must not be accepted on the
    strength of the token filename.
    """
    try:
        pwd.getpwnam(ABSENT_SOURCE_USER)
        raise AssertionError(f"{ABSENT_SOURCE_USER} unexpectedly exists; pick another name")
    except KeyError:
        pass

    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        # A perfectly well-formed regular token, owned by whoever runs the tests.
        _seed_source_token(tmp_path / "home", user=ABSENT_SOURCE_USER)
        outcome, runner, _output, home_base, _project_dir = _provision(
            tmp_path, agy_credential_source=ABSENT_SOURCE_USER
        )
        seeded_exists = _seeded_token_path(home_base).exists()

    assert isinstance(outcome, SystemExit)
    message = str(outcome)
    assert f"agy_credential_source {ABSENT_SOURCE_USER!r} is not a user on this machine" in message
    assert "only from an existing account that owns it" in message
    assert not seeded_exists
    assert _mutated(runner) == []


def test_failed_ownership_assignment_rolls_back_the_partial_token() -> None:
    """A token of unverified ownership must not survive: it would read as already
    present on the retry and suppress the seed that fixes it."""
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        outcome, _runner, _output, home_base, _project_dir = _provision(
            tmp_path,
            agy_credential_source=SEED_SOURCE_USER,
            failing_chown_fragment="/fd/",
        )
        leftover = _seeded_token_path(home_base).exists()

    assert isinstance(outcome, SystemExit)
    assert "failed to assign" in str(outcome)
    assert not leftover


def test_failed_seed_leaves_the_credential_absent_for_the_retry() -> None:
    """After a failed seed the state a retry starts from must be 'absent', not 'installed'.

    Paired with test_existing_owner_with_absent_credential_is_seeded below, this is the
    retry invariant: the failure rolls back to absent, and absent-plus-existing-owner
    seeds. The two are asserted separately because a single end-to-end retry is not
    reproducible here -- _existing_project_path_is_usable is a real uid check against the
    project directory the first attempt left behind, and the fixture's owner user is not
    a real account, so the second run is refused before it reaches the seeding decision.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        outcome, _runner, _output, home_base, _project_dir = _provision(
            tmp_path,
            agy_credential_source=SEED_SOURCE_USER,
            failing_chown_fragment="/fd/",
        )
        state = team_launcher._agy_credential_state("otto-agent", home_base)

    assert isinstance(outcome, SystemExit)
    assert state == team_launcher.AGY_CREDENTIAL_ABSENT


def test_existing_owner_with_absent_credential_is_seeded() -> None:
    """The state a retry resumes from: owner already exists, credential not installed."""
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        outcome, runner, output, home_base, project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER, owner_exists=True
        )
        seeded = _seeded_token_path(home_base)
        seeded_text = seeded.read_text(encoding="utf-8") if seeded.exists() else ""
        artifact = json.loads(
            (project_dir / ".switchyard" / "porter.project.json").read_text(encoding="utf-8")
        )
        chown_calls = _token_chown_calls(runner)

    assert outcome == 0
    assert seeded_text == SEED_TOKEN_TEXT
    assert chown_calls, "an existing owner without a credential must be seeded, not skipped"
    assert artifact["project"]["capability_grants"]["agy_credential_source"] == SEED_SOURCE_USER
    assert f"seeded agy credential for otto-agent from {SEED_SOURCE_USER}" in output


def test_existing_owner_without_reuse_consent_never_reaches_seeding() -> None:
    """Consent to reuse an existing owner is enforced upstream, before the seeding step."""
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        outcome, runner, _output, home_base, _project_dir = _provision(
            tmp_path,
            agy_credential_source=SEED_SOURCE_USER,
            owner_exists=True,
            allow_existing_owner_user=False,
        )
        seeded_exists = _seeded_token_path(home_base).exists()

    assert isinstance(outcome, SystemExit)
    assert "cancelled" in str(outcome)
    assert not seeded_exists
    assert _mutated(runner) == []


def _write_target(home_base: Path, *, text: str = "existing\n", mode: int = 0o600) -> Path:
    target = _seeded_token_path(home_base)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    target.chmod(mode)
    return target


def test_target_owned_by_another_user_is_refused_not_reported_installed() -> None:
    """A 0600 token the pane owner cannot read must never count as installed.

    Otherwise provisioning completes and records a credential source while agy login
    still cannot succeed, which is this ticket's own bug. The owner user here is real and
    its uid differs from the test runner's, so the mismatch is genuine rather than staged.
    """
    assert pwd.getpwnam(OWNER_USER).pw_uid != os.getuid(), "fixture needs a real uid mismatch"

    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        _seed_source_token(home_base)
        _write_target(home_base, text="owned-by-someone-else\n", mode=0o600)

        outcome, runner, _output, home_base, _project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER, owner_exists=True
        )
        preserved = _seeded_token_path(home_base).read_text(encoding="utf-8")

    assert isinstance(outcome, SystemExit)
    message = str(outcome)
    assert f"is not a 0600 regular file owned by {OWNER_USER}" in message
    assert "agy could not read it" in message
    assert not _token_chown_calls(runner)
    # Refused, not silently repaired or overwritten.
    assert preserved == "owned-by-someone-else\n"


def test_credential_directory_swapped_for_a_symlink_is_refused() -> None:
    """The owner controls this directory and can replace it after install(1) returns.

    Naming the path again would let a root-run write land outside the owner's home, so
    the directory is walked with O_NOFOLLOW and every later step uses that descriptor.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        escape = tmp_path / "outside-the-home"
        escape.mkdir()
        _seed_source_token(tmp_path / "home")

        outcome, runner, _output, home_base, _project_dir = _provision(
            tmp_path,
            agy_credential_source=SEED_SOURCE_USER,
            swap_credential_dir_to=escape,
        )
        escaped_files = sorted(p.name for p in escape.iterdir())
        swap_happened = (home_base / OWNER_USER / ".gemini" / "antigravity-cli").is_symlink()

    assert isinstance(outcome, SystemExit)
    assert "replaced path component" in str(outcome)
    assert escaped_files == [], f"credential written outside the owner home: {escaped_files}"
    assert swap_happened, "fixture did not actually perform the swap"
    assert not _token_chown_calls(runner)


def test_world_readable_existing_target_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        _seed_source_token(home_base)
        _write_target(home_base, mode=0o644)

        outcome, runner, _output, _home_base, _project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER, owner_exists=True
        )

    assert isinstance(outcome, SystemExit)
    assert "is not a 0600 regular file owned by" in str(outcome)
    assert not _token_chown_calls(runner)


def test_credential_state_requires_a_private_file_owned_by_the_owner() -> None:
    """Unit coverage for the installed/absent/unusable classification.

    The 'installed' verdict is asserted here rather than through a full provision: it
    needs a target owned by the project owner, and the harness can only create files
    owned by the test runner, so the owner has to be the runner itself -- which cannot
    also be the credential source, since seeding from the owner is refused.
    """
    state = team_launcher._agy_credential_state
    runner_user = team_launcher.current_user_name()

    with tempfile.TemporaryDirectory(prefix="pgu-agy-state.") as tmp:
        home_base = Path(tmp) / "home"
        target = home_base / runner_user / ".gemini" / "antigravity-cli" / TOKEN_NAME

        assert state(runner_user, home_base) == team_launcher.AGY_CREDENTIAL_ABSENT

        target.parent.mkdir(parents=True)
        target.write_text("token\n", encoding="utf-8")
        target.chmod(0o600)
        assert state(runner_user, home_base) == team_launcher.AGY_CREDENTIAL_INSTALLED

        # Same file, a different real owner: audit's reproduction.
        assert state(OWNER_USER, home_base.parent / "home") == team_launcher.AGY_CREDENTIAL_ABSENT
        other_base = Path(tmp) / "other"
        other_target = other_base / OWNER_USER / ".gemini" / "antigravity-cli" / TOKEN_NAME
        other_target.parent.mkdir(parents=True)
        other_target.write_text("token\n", encoding="utf-8")
        other_target.chmod(0o600)
        assert state(OWNER_USER, other_base) == team_launcher.AGY_CREDENTIAL_UNUSABLE

        target.chmod(0o644)
        assert state(runner_user, home_base) == team_launcher.AGY_CREDENTIAL_UNUSABLE
        target.chmod(0o600)

        # An owner with no passwd entry gives nothing to compare against, so it fails closed.
        absent_base = Path(tmp) / "absent"
        absent_target = absent_base / ABSENT_SOURCE_USER / ".gemini" / "antigravity-cli" / TOKEN_NAME
        absent_target.parent.mkdir(parents=True)
        absent_target.write_text("token\n", encoding="utf-8")
        absent_target.chmod(0o600)
        assert state(ABSENT_SOURCE_USER, absent_base) == team_launcher.AGY_CREDENTIAL_UNUSABLE


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


def _artifact_grants(project_dir: Path) -> dict:
    raw = json.loads((project_dir / ".switchyard" / "porter.project.json").read_text(encoding="utf-8"))
    return raw["project"]["capability_grants"]


def test_recorded_host_source_is_used_without_a_per_project_flag() -> None:
    """The point of the host setting: no ceremony on each provision."""
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        team_launcher.write_host_agy_credential_source(
            SEED_SOURCE_USER, settings_path=_host_setting_path(tmp_path)
        )

        outcome, runner, output, home_base, project_dir = _provision(
            tmp_path, agy_credential_source=None
        )
        seeded = _seeded_token_path(home_base)
        seeded_text = seeded.read_text(encoding="utf-8") if seeded.exists() else ""
        grants = _artifact_grants(project_dir)

    assert outcome == 0
    assert seeded_text == SEED_TOKEN_TEXT
    assert _token_chown_calls(runner)
    assert grants["agy_credential_source"] == SEED_SOURCE_USER
    assert grants["agy_credential_source_origin"] == "host_default"
    assert f"seeded agy credential for {OWNER_USER} from {SEED_SOURCE_USER}" in output


def test_per_project_flag_overrides_the_host_source_and_is_recorded_as_such() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        team_launcher.write_host_agy_credential_source(
            "root", settings_path=_host_setting_path(tmp_path)
        )

        outcome, _runner, _output, _home_base, project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER
        )
        grants = _artifact_grants(project_dir)

    assert outcome == 0
    assert grants["agy_credential_source"] == SEED_SOURCE_USER
    assert grants["agy_credential_source_origin"] == "project_override"


def test_opt_out_ignores_the_host_source_and_seeds_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        team_launcher.write_host_agy_credential_source(
            SEED_SOURCE_USER, settings_path=_host_setting_path(tmp_path)
        )

        outcome, runner, output, home_base, project_dir = _provision(
            tmp_path, agy_credential_source=None, no_agy_credential=True
        )
        seeded_exists = _seeded_token_path(home_base).exists()
        grants = _artifact_grants(project_dir)

    assert outcome == 0
    assert not seeded_exists
    assert not _token_chown_calls(runner)
    assert grants["agy_credential_source"] == ""
    assert grants["agy_credential_source_origin"] == "opt_out"
    assert "seeded agy credential" not in output


def test_noninteractive_never_infers_an_account_when_nothing_is_recorded() -> None:
    """--yes must not silently fall back to SUDO_USER or the current user."""
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")  # a usable token exists for the invoking user
        assert not _host_setting_path(tmp_path).exists()

        outcome, runner, output, home_base, project_dir = _provision(
            tmp_path, agy_credential_source=None
        )
        seeded_exists = _seeded_token_path(home_base).exists()
        grants = _artifact_grants(project_dir)
        recorded_anything = _host_setting_path(tmp_path).exists()

    assert outcome == 0
    assert not seeded_exists, "a source was inferred without being configured"
    assert not _token_chown_calls(runner)
    assert grants["agy_credential_source"] == ""
    assert grants["agy_credential_source_origin"] == "unset"
    assert not recorded_anything
    assert "seeded agy credential" not in output


def test_interactive_proposal_requires_confirmation_and_records_it() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        setting = _host_setting_path(tmp_path)

        # Declining records nothing and seeds nothing.
        with _invoking_human(SEED_SOURCE_USER):
            declined, runner, output, home_base, project_dir = _provision(
                tmp_path, agy_credential_source=None, yes=False, answers={"Record": "n", "Proceed": "y"}
            )
        declined_recorded = setting.exists()
        declined_seeded = _seeded_token_path(home_base).exists()
        declined_grants = _artifact_grants(project_dir) if declined == 0 else {}

    assert declined == 0, output[-400:]
    assert not declined_recorded
    assert not declined_seeded
    assert declined_grants.get("agy_credential_source_origin") == "unset"
    assert "no agy credential source is recorded for this host" in output
    assert "set one later with" in output


def test_interactive_confirmation_records_the_host_source() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        setting = _host_setting_path(tmp_path)

        with _invoking_human(SEED_SOURCE_USER):
            outcome, _runner, output, home_base, project_dir = _provision(
                tmp_path, agy_credential_source=None, yes=False, answers={"Record": "y", "Proceed": "y"}
            )
        recorded = json.loads(setting.read_text(encoding="utf-8")) if setting.exists() else {}
        seeded_exists = _seeded_token_path(home_base).exists()
        grants = _artifact_grants(project_dir) if outcome == 0 else {}

    assert outcome == 0, output[-400:]
    assert recorded.get("source_user") == SEED_SOURCE_USER
    assert seeded_exists
    assert grants.get("agy_credential_source_origin") == "host_default"


def test_existing_owner_confirmation_states_the_shared_account() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-seed.") as tmp:
        tmp_path = Path(tmp)
        _seed_source_token(tmp_path / "home")
        _outcome, _runner, output, _home_base, _project_dir = _provision(
            tmp_path, agy_credential_source=SEED_SOURCE_USER, owner_exists=True
        )

    assert f"the agy credential from {SEED_SOURCE_USER} will be installed into {OWNER_USER}" in output
    assert "every role of this project will be able to act as the Google account" in output


def test_host_setting_show_set_and_clear() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-agy-host.") as tmp:
        setting = Path(tmp) / "agy-credential.json"
        lines: list[str] = []

        team_launcher.switchyard_agy_credential_command(
            "show", settings_path=setting, print_func=lines.append
        )
        assert "no agy credential source recorded" in lines[-1]

        team_launcher.switchyard_agy_credential_command(
            "set", source_user=SEED_SOURCE_USER, settings_path=setting, print_func=lines.append
        )
        assert team_launcher.read_host_agy_credential_source(setting) == SEED_SOURCE_USER
        assert any("act as the Google account" in line for line in lines)

        team_launcher.switchyard_agy_credential_command(
            "show", settings_path=setting, print_func=lines.append
        )
        assert f"agy credential source: {SEED_SOURCE_USER}" in lines[-1]

        team_launcher.switchyard_agy_credential_command(
            "clear", settings_path=setting, print_func=lines.append
        )
        assert not setting.exists()
        assert team_launcher.read_host_agy_credential_source(setting) == ""

        try:
            team_launcher.switchyard_agy_credential_command(
                "set", source_user=ABSENT_SOURCE_USER, settings_path=setting, print_func=lines.append
            )
            raise AssertionError("expected refusal for a non-existent user")
        except SystemExit as exc:
            assert "is not a user on this machine" in str(exc)


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_agy_credential_seeding_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
