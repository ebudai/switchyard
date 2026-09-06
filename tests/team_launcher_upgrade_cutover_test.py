#!/usr/bin/env python3
"""SYRD-45: a tenant upgrade must be ordered, journaled and identity-safe.

The rollout this covers rewrote a declarative tenant's configuration to name
per-role Unix accounts before those accounts existed, then tried to make a
director-authority board write from the root upgrade process. The first leaves
roles that cannot start and panes the board would stop recognising; the second
cannot succeed without impersonating the director.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from team_launcher_test_helpers import *


class _RunningTenant:
    """Model the part the mocks kept out: processes with real uids.

    Each role's pane is a real pid whose uid the kernel reports, and the
    verification compares that to the uid the account resolves to. Nothing here
    stands in for the uid read itself.
    """

    def __init__(self, config_path: Path, *, account_uid: int):
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.targets = [role["target"] for role in payload["roles"]]
        self.sessions = {role["tmux_session"] for role in payload["roles"]}
        self.commands = {role["target"]: role["live_commands"][0] for role in payload["roles"]}
        self.account_uid = account_uid
        self.pane_pid = os.getpid()
        self.launches: list[str] = []
        self.stops: list[str] = []
        self.live = True
        self.board_writes: list[list[str]] = []
        self.listener_calls: list[str] = []
        self.listener_active = True

    def runner(self):
        """A runner whose answers follow the stop and start, as a host's would."""
        inner = _AnyRoleSudoRunner(
            existing_sessions=set(self.sessions),
            current_commands=dict(self.commands),
            pane_pids={target: self.pane_pid for target in self.targets},
        )
        tenant = self

        def call(args, **kwargs):
            argv = list(args)
            bare = argv[4:] if argv[:2] == ["sudo", "-u"] and len(argv) > 4 else argv
            if not tenant.live and bare[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            if argv[:1] == ["install"] and len(argv) >= 3:
                Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(argv[-2], argv[-1])
                return subprocess.CompletedProcess(argv, 0, "", "")
            joined = " ".join(argv)
            if "notify-listener" in joined:
                tenant.listener_calls.append(joined)
                if "is-active" in joined:
                    return subprocess.CompletedProcess(
                        argv, 0 if tenant.listener_active else 3,
                        "active\n" if tenant.listener_active else "inactive\n", "",
                    )
                if "stop" in joined:
                    tenant.listener_active = False
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if "restart" in joined or "start" in joined:
                    tenant.listener_active = True
                    return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(argv, 0, "active\n", "")
            if argv[:1] == ["systemctl"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if bare and Path(bare[0]).name == "ticket-board-write":
                tenant.board_writes.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return inner(argv, **kwargs)

        return call

    def __enter__(self):
        self._uid = team_launcher.uid_for_user
        self._launch = team_launcher.launch_project
        self._stop = team_launcher.stop_project
        self._exists = team_launcher.local_account_exists
        team_launcher.local_account_exists = lambda account: account.startswith("porter-")
        team_launcher.uid_for_user = lambda name: (
            self.account_uid if name.startswith("porter-") else self._uid(name)
        )
        def _launch(config, **_kwargs):
            self.launches.append(config.project)
            self.live = True
            return 0

        # The production start path no longer opens a window; it starts each
        # role's session under its own account.
        self._start_sessions = team_launcher._start_role_sessions_without_a_window
        team_launcher._start_role_sessions_without_a_window = _launch

        def _stop(config, **_kwargs):
            self.stops.append(config.project)
            self.live = False
            return 0

        team_launcher.launch_project = _launch
        team_launcher.stop_project = _stop
        return self

    def __exit__(self, *_exc):
        team_launcher.uid_for_user = self._uid
        team_launcher.launch_project = self._launch
        team_launcher.stop_project = self._stop
        team_launcher.local_account_exists = self._exists
        team_launcher._start_role_sessions_without_a_window = self._start_sessions
        return False


class _FakeBoard:
    """Stand in for the running board's /api/workflow, which decides completion."""

    def __init__(self, document):
        self.document = document

    def __call__(self, url):
        board = self

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, *_args):
                return json.dumps({"revision": 8, "document": board.document})

        return _Response()


def _board_with_marker(migrated: bool):
    return _FakeBoard({"roles": [], "migrations": {"director_onboarding": migrated}})


def _unreachable_board(url):
    raise OSError("connection refused")


def _mark_projection_migrated(config_path: Path) -> None:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["workflow"]["migrations"] = {"director_onboarding": True}
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _privileged_root(tmp: Path) -> Path:
    """Point root's own records and unit directory at a sandbox instead of /etc."""
    root = tmp / "etc-switchyard"
    os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(root)
    units = tmp / "systemd"
    units.mkdir(exist_ok=True)
    team_launcher.SYSTEMD_UNIT_DIR = units
    return root


def _stage_units(project: str) -> None:
    """The generated units the transaction installs, as the artifacts phase leaves them."""
    staged = team_launcher.privileged_provision_dir(
        project, root=team_launcher.switchyard_privileged_provision_root()
    )
    staged.mkdir(parents=True, exist_ok=True)
    for unit in (
        f"{project}-ticket-board.service",
        f"{project}-ticket-board-notify-listener.service",
    ):
        (staged / unit).write_text(f"[Service]\n# generated {unit}\n", encoding="utf-8")


def _deployed_release(tmp: Path, project: str, sha: str) -> Path:
    """A board root whose `current` release is ``sha``, as a deploy leaves it."""
    board_root = tmp / f"{project}-ticketboard-live"
    release = board_root / "releases" / sha
    release.mkdir(parents=True, exist_ok=True)
    (release / ".pgu-deploy-sha").write_text(sha + "\n", encoding="utf-8")
    current = board_root / "current"
    if not current.exists() and not current.is_symlink():
        current.symlink_to(release)
    return board_root


def _declarative_tenant(
    tmp: Path, *, project: str = "porter", accounts: bool = False, board_root: Path | None = None
) -> tuple[Path, Path]:
    _privileged_root(tmp)
    _stage_units(project)
    config_path = _write_six_visible_role_config(tmp, project=project)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    # A real account: the privileged paths chown generated files to the project
    # owner, and this suite runs the root branch.
    payload["run_as_user"] = team_launcher.current_user_name()
    payload["workflow"] = {"roles": [{"name": role["role"], "active": True} for role in payload["roles"]]}
    # Liveness is a real probe: it reads the pane's pid and looks for the role's
    # command in that process's actual tree. Naming this interpreter makes the
    # test's own pid a genuinely live role rather than a claimed one.
    for role in payload["roles"]:
        role["live_commands"] = ["python3"]
    if accounts:
        for role in payload["roles"]:
            role["run_as_user"] = f"{project}-{role['role']}"
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The recorded owner home. Without it the transaction resolves the owner's
    # user-unit directory from passwd, which is the real home on this host --
    # a test must not install anything there.
    owner_home = tmp / "home"
    owner_home.mkdir(exist_ok=True)
    plan = {"project": project, "owner_user": payload["run_as_user"], "owner_home": str(owner_home)}
    if board_root is not None:
        # Without a board root there is no release to read at all, and the release
        # phase has nothing to derive from (SYRD-48).
        plan["board_root"] = str(board_root)
    (tmp / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return config_path, tmp


class _AnyRoleSudoRunner(FakeRunner):
    """A tenant whose roles are running now, whichever account probes them.

    Once a role declares its own account every probe is wrapped in `sudo -u
    <account>`, so a runner that only understands bare tmux argv reports the
    project as stopped -- which is exactly the state in which a revert would be
    skipped.
    """

    def __call__(self, args, **kwargs):
        if args[:2] == ["sudo", "-u"] and len(args) > 3:
            inner = args[4:] if args[3] == "-H" else args[3:]
            return super().__call__(inner, **kwargs)
        return super().__call__(args, **kwargs)


def _live_runner(config_path: Path) -> FakeRunner:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    sessions = {role["tmux_session"] for role in payload["roles"]}
    commands = {role["target"]: role["live_commands"][0] for role in payload["roles"]}
    return _AnyRoleSudoRunner(existing_sessions=sessions, current_commands=commands)


def _upgrade(
    config_path: Path,
    *,
    as_root: bool,
    exists: set[str] | None = None,
    dry_run: bool = False,
    runner: FakeRunner | None = None,
    board=None,
    source_repo: Path | None = None,
    deploy_ref: str = team_launcher.DEFAULT_TENANT_RELEASE_DEPLOY_REF,
):
    """Run the privileged upgrade with the host facts stated explicitly."""
    printed: list[str] = []
    known = exists if exists is not None else set()
    original_euid = team_launcher.os.geteuid
    original_exists = team_launcher.local_account_exists
    original_migrate = team_launcher.migrate_declarative_director_onboarding
    original_opener = team_launcher._open_board_url
    migrations: list[str] = []
    try:
        team_launcher._open_board_url = board or _unreachable_board
        team_launcher.os.geteuid = (lambda: 0) if as_root else original_euid
        team_launcher.local_account_exists = lambda account: account in known
        team_launcher.migrate_declarative_director_onboarding = (
            lambda config, **kwargs: migrations.append(config.project) or True
        )
        config = team_launcher.load_project_config(
            json.loads(config_path.read_text(encoding="utf-8"))["project"], config_path
        )
        result = team_launcher.upgrade_project_command(
            config,
            config_path=config_path,
            dry_run=dry_run,
            source_repo=source_repo,
            deploy_ref=deploy_ref,
            runner=runner or FakeRunner(),
            print_func=printed.append,
        )
    finally:
        team_launcher.os.geteuid = original_euid
        team_launcher.local_account_exists = original_exists
        team_launcher.migrate_declarative_director_onboarding = original_migrate
        team_launcher._open_board_url = original_opener
    return result, "\n".join(printed), migrations


def test_root_upgrade_never_attempts_the_director_write() -> None:
    """No caller role exists for root, and manufacturing one would be impersonation."""
    with tempfile.TemporaryDirectory(prefix="upgrade-root-director.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        for name in ("TICKET_BOARD_CALLER_ROLE", "TICKET_BOARD_URL"):
            os.environ.pop(name, None)
        result, output, migrations = _upgrade(config_path, as_root=True)
        assert result == 0, output
        assert migrations == [], migrations
        assert "caller_role" not in output, output
        assert "finish-upgrade porter" in output, output
        assert "root cannot make that write" in output, output


def test_the_director_write_refuses_root_and_records_the_director() -> None:
    with tempfile.TemporaryDirectory(prefix="director-phase.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        config = team_launcher.load_project_config("porter", config_path)
        original_euid = team_launcher.os.geteuid
        printed: list[str] = []
        try:
            team_launcher.os.geteuid = lambda: 0
            try:
                team_launcher.migrate_declarative_director_onboarding(config, config_path=config_path)
                raise AssertionError("root was allowed to make the director's board write")
            except SystemExit as exc:
                assert "cannot be made by root" in str(exc), exc
            assert team_launcher.finish_upgrade_command(
                config, config_path=config_path, print_func=printed.append
            ) == 1
            assert "must not run as root" in "\n".join(printed), printed
        finally:
            team_launcher.os.geteuid = original_euid

        # The director's own process performs it, and completion is read from
        # the board and the projection rather than from the call's return value.
        printed.clear()
        original_migrate = team_launcher.migrate_declarative_director_onboarding
        original_opener = team_launcher._open_board_url
        try:
            team_launcher.migrate_declarative_director_onboarding = (
                lambda config, **kwargs: _mark_projection_migrated(config_path) or True
            )
            team_launcher._open_board_url = _board_with_marker(True)
            assert team_launcher.finish_upgrade_command(
                config, config_path=config_path, runner=FakeRunner(), print_func=printed.append
            ) == 0
        finally:
            team_launcher.migrate_declarative_director_onboarding = original_migrate
            team_launcher._open_board_url = original_opener
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "director") == "done", journal


def test_a_board_that_cannot_accept_the_migration_is_not_recorded_as_done() -> None:
    """`migrated=False` is also what a board too old to accept the document returns."""
    with tempfile.TemporaryDirectory(prefix="director-pre-phase-one.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        config = team_launcher.load_project_config("porter", config_path)
        printed: list[str] = []
        original_migrate = team_launcher.migrate_declarative_director_onboarding
        original_opener = team_launcher._open_board_url
        try:
            # The real behaviour on a pre-phase-one board: it reports nothing
            # migrated and the document keeps no marker.
            team_launcher.migrate_declarative_director_onboarding = lambda config, **kwargs: False
            team_launcher._open_board_url = _board_with_marker(False)
            result = team_launcher.finish_upgrade_command(
                config, config_path=config_path, runner=FakeRunner(), print_func=printed.append
            )
        finally:
            team_launcher.migrate_declarative_director_onboarding = original_migrate
            team_launcher._open_board_url = original_opener
        output = "\n".join(printed)
        assert result == 1, output
        assert "has not landed" in output, output
        assert "deploy-restart" not in output, output
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "director") == "pending", journal


def test_one_complete_legacy_sequence_across_successive_invocations() -> None:
    """The whole ordered path a legacy tenant actually takes, run end to end."""
    with tempfile.TemporaryDirectory(prefix="legacy-sequence.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
        accounts = {f"porter-{role}" for role in roles}
        board_without_marker = _board_with_marker(False)

        # 1. Accounts do not exist: artifacts refresh, the operator artifact is
        #    written, nothing else happens, and no deploy is suggested.
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, first, _m = _upgrade(
                config_path, as_root=True, exists=set(), runner=tenant.runner(),
                board=board_without_marker,
            )
        assert "still share the project account" in first, first
        assert "deploy-restart" not in first, first
        assert not any(
            role.get("run_as_user")
            for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]
        )

        # 2. The operator creates them. The next invocation cuts over -- with the
        #    director phase still outstanding, which must not block it.
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, second, _m = _upgrade(
                config_path, as_root=True, exists=accounts, runner=tenant.runner(),
                board=board_without_marker,
            )
        assert "running under per-role identities" in second, second
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert all(role.get("run_as_user") == f"porter-{role['role']}" for role in after["roles"]), after
        # Only now is the release deploy offered, and the director is named next.
        assert "withholding" not in second, second
        assert "finish-upgrade porter" in second, second

        # 3. The director makes its own write, from its own account.
        config = team_launcher.load_project_config("porter", config_path)
        printed: list[str] = []
        original_migrate = team_launcher.migrate_declarative_director_onboarding
        original_opener = team_launcher._open_board_url
        try:
            team_launcher.migrate_declarative_director_onboarding = (
                lambda config, **kwargs: _mark_projection_migrated(config_path) or True
            )
            team_launcher._open_board_url = _board_with_marker(True)
            assert team_launcher.finish_upgrade_command(
                config, config_path=config_path, runner=FakeRunner(), print_func=printed.append
            ) == 0, printed
        finally:
            team_launcher.migrate_declarative_director_onboarding = original_migrate
            team_launcher._open_board_url = original_opener
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "director") == "done", journal


class _OwnerOnlyRunner(FakeRunner):
    """The live tenant exactly: sessions exist, but only under the project account.

    Probes wrapped in `sudo -u <role account>` fail, because those accounts do
    not exist; the same probe as the project account answers. A runner that
    answers both hides the misdetection this covers.
    """

    def __init__(self, owner: str, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner

    def __call__(self, args, **kwargs):
        argv = list(args)
        if argv[:2] == ["sudo", "-u"]:
            if argv[2] != self.owner:
                return subprocess.CompletedProcess(argv, 1, "", "")
            argv = argv[4:] if len(argv) > 3 and argv[3] == "-H" else argv[3:]
        return super().__call__(argv, **kwargs)


def _owner_only_runner(config_path: Path) -> FakeRunner:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return _OwnerOnlyRunner(
        payload["run_as_user"],
        existing_sessions={role["tmux_session"] for role in payload["roles"]},
        current_commands={role["target"]: role["live_commands"][0] for role in payload["roles"]},
    )


def test_the_live_partial_tenant_is_not_mistaken_for_a_fresh_one() -> None:
    """Six sessions under the project account, six accounts that do not exist."""
    with tempfile.TemporaryDirectory(prefix="live-partial.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        config = team_launcher.load_project_config("porter", config_path)
        runner = _owner_only_runner(config_path)

        # Probing only through the configured accounts sees nothing at all.
        assert [role.role for role in team_launcher._running_project_roles(config, runner=runner)] == []
        # Probing both identities sees every role, served by the project account.
        serving = team_launcher.running_role_identities(config, runner=runner)
        assert set(serving) == {role.role for role in config.roles}, serving
        assert set(serving.values()) == {config.run_as_user}, serving

        result, output, _m = _upgrade(config_path, as_root=True, exists=set(), runner=runner)
        assert result == 0, output
        assert "left as provisioned" not in output, output
        assert "do not agree" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert not any(role.get("run_as_user") for role in after["roles"]), after


def test_the_observed_partial_state_is_detected_and_reverted() -> None:
    """Config names per-role accounts, none exist, sessions still share the owner."""
    with tempfile.TemporaryDirectory(prefix="upgrade-partial.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        before = json.loads(config_path.read_text(encoding="utf-8"))
        assert all(role.get("run_as_user") for role in before["roles"])

        result, output, _migrations = _upgrade(
            config_path, as_root=True, exists=set(), runner=_live_runner(config_path)
        )
        assert result == 0, output
        assert "do not agree" in output, output
        assert "porter-designer does not exist" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert not any(role.get("run_as_user") for role in after["roles"]), after
        config = team_launcher.load_project_config("porter", config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") in {"reverted", "pending"}, journal


def test_a_freshly_provisioned_project_is_left_as_provisioned() -> None:
    """Nothing is running, so there is no legacy identity to preserve or undo."""
    with tempfile.TemporaryDirectory(prefix="upgrade-fresh.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        before = json.loads(config_path.read_text(encoding="utf-8"))
        result, output, _m = _upgrade(config_path, as_root=True, exists=set(), runner=FakeRunner())
        assert result == 0, output
        assert "left as provisioned" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert [role.get("run_as_user") for role in after["roles"]] == [
            role.get("run_as_user") for role in before["roles"]
        ], after


def test_the_cutover_happens_only_once_every_account_exists() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-cutover.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]

        # Nothing is written into the configuration while the accounts are absent.
        _result, output, _m = _upgrade(config_path, as_root=True, exists=set())
        assert "still share the project account" in output, output
        assert not any(
            role.get("run_as_user")
            for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]
        )
        assert (config_path.with_name("porter-role-accounts.sh")).is_file()

        # Once they exist, the same command stops the workers, moves the
        # configuration, restarts them and checks the uid each role's process is
        # actually running as before anything authorizes it.
        accounts = {f"porter-{role}" for role in roles}
        # The cutover does not wait on the director: the director's own write
        # is made from the identity this creates, so it comes after.
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, output, _m = _upgrade(
                config_path, as_root=True, exists=accounts, runner=tenant.runner(),
                board=_board_with_marker(False),
            )
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert all(role.get("run_as_user") == f"porter-{role['role']}" for role in after["roles"]), after
        assert tenant.stops and tenant.launches, (tenant.stops, tenant.launches)
        assert "running under per-role identities" in output, output
        # The units the board serves were installed and restarted inside the
        # same transaction, and every role was proved able to write as itself.
        installed = team_launcher.SYSTEMD_UNIT_DIR / "porter-ticket-board.service"
        assert installed.is_file() and "generated" in installed.read_text(), installed
        assert {argv[2] for argv in tenant.board_writes} == set(accounts), tenant.board_writes


def test_a_cutover_whose_processes_keep_the_old_uid_is_rolled_back() -> None:
    """Accounts existing is not the same as the workers running as them."""
    with tempfile.TemporaryDirectory(prefix="upgrade-rollback.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
        accounts = {f"porter-{role}" for role in roles}
        # The director phase is complete, so the cutover is allowed to run.
        _mark_projection_migrated(config_path)
        before = config_path.read_bytes()
        # The accounts resolve to a uid nothing is running as: the panes kept
        # the shared identity, which is exactly the state that would have made
        # the board reject every live role.
        original_opener = team_launcher._open_board_url
        team_launcher._open_board_url = _board_with_marker(True)
        with _RunningTenant(config_path, account_uid=os.getuid() + 4242) as tenant:
            result = team_launcher.cutover_role_identities_command(
                team_launcher.load_project_config("porter", config_path),
                config_path=config_path,
                runner=tenant.runner(),
                print_func=lambda _text: None,
            )
        team_launcher._open_board_url = original_opener
        assert result == 1
        assert config_path.read_bytes() == before, "the configuration was not put back"
        assert len(tenant.stops) == 2 and len(tenant.launches) == 2, (tenant.stops, tenant.launches)
        # The installed unit went back with everything else: there was none
        # before, so there is none after.
        assert not (team_launcher.SYSTEMD_UNIT_DIR / "porter-ticket-board.service").exists()
        config = team_launcher.load_project_config("porter", config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") == "rolled back", journal


def test_the_listener_is_stopped_before_the_release_and_its_state_restored() -> None:
    """It is the owner's user unit, it reads the schema the release changes."""
    for started_active in (True, False):
        with tempfile.TemporaryDirectory(prefix=f"listener-{started_active}.") as tmp:
            config_path, _ = _declarative_tenant(Path(tmp))
            roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
            accounts = {f"porter-{role}" for role in roles}
            printed: list[str] = []
            with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                tenant.listener_active = started_active
                # A cutover that fails after the listener came down.
                result = team_launcher.cutover_role_identities_command(
                    team_launcher.load_project_config("porter", config_path),
                    config_path=config_path,
                    runner=tenant.runner(),
                    launcher=lambda config, **kwargs: 1,
                    stopper=lambda config, **kwargs: (tenant.stops.append("x") or setattr(tenant, "live", False) or 0),
                    print_func=printed.append,
                )
            assert result == 1, printed
            assert any("stop" in call for call in tenant.listener_calls), tenant.listener_calls
            # Whatever it was before, that is what it is after.
            assert tenant.listener_active is started_active, (
                started_active, tenant.listener_calls
            )


def test_a_presentation_that_will_not_reconnect_rolls_back_in_place() -> None:
    """Blank slots are a failed cutover, and the rollback must not open a window."""
    with tempfile.TemporaryDirectory(prefix="presentation-failure.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        before = config_path.read_bytes()
        printed: list[str] = []
        original_reconnect = team_launcher.reconnect_presentation
        attempts: list[str] = []
        windows: list[str] = []
        try:
            def _failing(config, **kwargs):
                attempts.append(config.project)
                # Fails the forward pass, and again during the rollback, which
                # must be recorded rather than swallowed.
                return ["display slot 0 could not be re-pointed"]

            team_launcher.reconnect_presentation = _failing
            with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                original_launch = team_launcher.launch_project
                team_launcher.launch_project = lambda *a, **k: windows.append("window") or 0
                try:
                    result = team_launcher.cutover_role_identities_command(
                        team_launcher.load_project_config("porter", config_path),
                        config_path=config_path,
                        runner=tenant.runner(),
                        print_func=printed.append,
                    )
                finally:
                    team_launcher.launch_project = original_launch
        finally:
            team_launcher.reconnect_presentation = original_reconnect
        output = "\n".join(printed)
        assert result == 1, output
        assert "the presentation did not reconnect" in output, output
        assert "during the rollback" in output, output
        assert config_path.read_bytes() == before
        # The workers came back through the session path, not by relaunching the
        # project: no GUI window was opened by either pass.
        assert windows == [], windows
        assert len(attempts) == 2, attempts


def test_a_listener_that_will_not_start_rolls_the_whole_thing_back() -> None:
    with tempfile.TemporaryDirectory(prefix="listener-start-failure.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        before = config_path.read_bytes()
        printed: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            tenant.listener_active = True
            inner = tenant.runner()

            def refuses_to_start(args, **kwargs):
                joined = " ".join(args)
                if "notify-listener" in joined and ("restart" in joined or " start " in joined):
                    return subprocess.CompletedProcess(list(args), 1, "", "unit failed")
                return inner(args, **kwargs)

            result = team_launcher.cutover_role_identities_command(
                team_launcher.load_project_config("porter", config_path),
                config_path=config_path,
                runner=refuses_to_start,
                print_func=printed.append,
            )
        output = "\n".join(printed)
        assert result == 1, output
        assert "could not start" in output, output
        assert config_path.read_bytes() == before


def test_a_listener_that_will_not_stop_blocks_the_release() -> None:
    with tempfile.TemporaryDirectory(prefix="listener-stuck.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
        accounts = {f"porter-{role}" for role in roles}
        before = config_path.read_bytes()
        printed: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            inner = tenant.runner()

            def stuck(args, **kwargs):
                joined = " ".join(args)
                if "notify-listener" in joined and "stop" in joined:
                    return subprocess.CompletedProcess(list(args), 1, "", "still running")
                return inner(args, **kwargs)

            result = team_launcher.cutover_role_identities_command(
                team_launcher.load_project_config("porter", config_path),
                config_path=config_path,
                runner=stuck,
                print_func=printed.append,
            )
        output = "\n".join(printed)
        assert result == 1, output
        assert "could not stop" in output, output
        assert config_path.read_bytes() == before


def test_a_stop_that_leaves_workers_running_changes_nothing() -> None:
    """A tree that changes hands under a live worker takes its write access away."""
    with tempfile.TemporaryDirectory(prefix="stop-failure.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        _mark_projection_migrated(config_path)
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
        before = config_path.read_bytes()
        printed: list[str] = []
        original_opener = team_launcher._open_board_url
        try:
            team_launcher._open_board_url = _board_with_marker(True)
            with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                chowns: list[list[str]] = []
                runner = tenant.runner()

                def watching(args, **kwargs):
                    if args[:2] == ["chown", "-R"]:
                        chowns.append(list(args))
                    return runner(args, **kwargs)

                result = team_launcher.cutover_role_identities_command(
                    team_launcher.load_project_config("porter", config_path),
                    config_path=config_path,
                    runner=watching,
                    # The stop reports success and leaves everything running,
                    # which is the case that matters: the sessions stay live.
                    stopper=lambda config, **kwargs: 0,
                    launcher=lambda config, **kwargs: 0,
                    print_func=printed.append,
                )
        finally:
            team_launcher._open_board_url = original_opener
        output = "\n".join(printed)
        assert result == 1, output
        assert "still running" in output, output
        assert "Nothing was changed" in output, output
        assert chowns == [], chowns
        assert config_path.read_bytes() == before


def test_the_deploy_instruction_is_withheld_until_every_phase_is_ready() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-deploy-gate.") as tmp:
        (Path(tmp) / "partial").mkdir()
        config_path, _ = _declarative_tenant(Path(tmp))
        # The release enforces the per-role table, so it is withheld until the
        # roles are actually running under those accounts.
        _result, output, _m = _upgrade(config_path, as_root=True, exists=set())
        assert "withholding" in output, output
        assert "running under their own accounts" in output, output
        assert "deploy-restart" not in output, output
        # And the director is still named as the phase that follows it.
        assert "finish-upgrade porter" in output, output

        # A tenant part-way onto per-role accounts withholds it too.
        partial_path, _ = _declarative_tenant(Path(tmp) / "partial", accounts=True)
        _result, output, _m = _upgrade(
            partial_path, as_root=True, exists={"porter-designer"}, runner=_live_runner(partial_path)
        )
        assert "do not agree" in output or "withholding" in output, output
        assert "deploy-restart" not in output, output


def test_dry_run_reports_every_phase_and_changes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-dry-run.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        before = config_path.read_bytes()
        result, output, _m = _upgrade(config_path, as_root=True, exists=set(), dry_run=True)
        assert result == 0, output
        assert config_path.read_bytes() == before
        assert not config_path.with_name("porter-upgrade.json").exists()
        for phase, owner, _detail in team_launcher.UPGRADE_PHASES:
            assert phase in output and owner in output, (phase, output)


def test_an_interrupted_upgrade_resumes_and_is_safe_to_retry() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-retry.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
        accounts = {f"porter-{role}" for role in roles}

        _result, _output, _m = _upgrade(config_path, as_root=True, exists=set())
        first = config_path.read_bytes()
        # Interrupted before the operator created the accounts: rerunning
        # changes nothing and repeats no instruction destructively.
        _result, _output, _m = _upgrade(config_path, as_root=True, exists=set())
        assert config_path.read_bytes() == first

        _mark_projection_migrated(config_path)
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, _output, _m = _upgrade(
                config_path, as_root=True, exists=accounts, runner=tenant.runner(),
                board=_board_with_marker(True),
            )
            cut = config_path.read_bytes()
            # Interrupted after the cutover: rerunning neither repeats the stop
            # and restart nor rewrites anything.
            stops_after_cutover = len(tenant.stops)
            _result, output, _m = _upgrade(
                config_path, as_root=True, exists=accounts, runner=tenant.runner(),
                board=_board_with_marker(True),
            )
            assert config_path.read_bytes() == cut, output
            assert len(tenant.stops) == stops_after_cutover, tenant.stops


def test_each_tenant_carries_its_own_director_phase() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-multi-tenant.") as tmp:
        tmp_path = Path(tmp)
        first_dir = tmp_path / "porter"
        second_dir = tmp_path / "atlas"
        first_dir.mkdir()
        second_dir.mkdir()
        first_path, _ = _declarative_tenant(first_dir, project="porter")
        second_path, _ = _declarative_tenant(second_dir, project="atlas")
        migrated: list[str] = []
        original_migrate = team_launcher.migrate_declarative_director_onboarding
        original_opener = team_launcher._open_board_url

        def _migrate(config, **kwargs):
            migrated.append(config.project)
            _mark_projection_migrated(first_path if config.project == "porter" else second_path)
            return True

        try:
            team_launcher.migrate_declarative_director_onboarding = _migrate
            team_launcher._open_board_url = _board_with_marker(True)
            for path, project in ((first_path, "porter"), (second_path, "atlas")):
                config = team_launcher.load_project_config(project, path)
                assert team_launcher.finish_upgrade_command(
                    config, config_path=path, runner=FakeRunner(), print_func=lambda _t: None
                ) == 0
        finally:
            team_launcher.migrate_declarative_director_onboarding = original_migrate
            team_launcher._open_board_url = original_opener
        assert migrated == ["porter", "atlas"], migrated
        for path, project in ((first_path, "porter"), (second_path, "atlas")):
            config = team_launcher.load_project_config(project, path)
            journal = team_launcher.read_upgrade_journal(config, config_path=path)
            assert journal["project"] == project, journal
            assert team_launcher.upgrade_phase_state(journal, "director") == "done", journal


def test_a_forged_tenant_journal_cannot_release_the_activation() -> None:
    """The tenant's copy of the phase record is readable, not authoritative."""
    with tempfile.TemporaryDirectory(prefix="forged-journal.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        config = team_launcher.load_project_config("porter", config_path)
        forged = {
            "schema": team_launcher.UPGRADE_JOURNAL_SCHEMA,
            "project": "porter",
            "phases": {
                "director": {"state": "done", "owner": "director", "at": "now", "detail": "forged"},
                "identities": {"state": "done", "owner": "root", "at": "now", "detail": "forged"},
            },
        }
        team_launcher.upgrade_journal_path(config, config_path=config_path).write_text(
            json.dumps(forged), encoding="utf-8"
        )
        # The board says otherwise, and the board is what is asked.
        original_opener = team_launcher._open_board_url
        try:
            team_launcher._open_board_url = _board_with_marker(False)
            state, reason = team_launcher.director_onboarding_state(config, config_path=config_path)
        finally:
            team_launcher._open_board_url = original_opener
        assert state == "pending", (state, reason)

        _result, output, _m = _upgrade(
            config_path, as_root=True, exists=set(), board=_board_with_marker(False)
        )
        assert "withholding" in output, output
        assert "deploy-restart" not in output, output
        trusted = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert team_launcher.upgrade_phase_state(trusted, "director") != "done", trusted
        # Root's record is a different file in a directory the tenant does not own.
        root_record = team_launcher.privileged_upgrade_journal_path(config)
        assert root_record.is_file(), root_record
        assert root_record != team_launcher.upgrade_journal_path(config, config_path=config_path)
        assert json.loads(root_record.read_text())["phases"]["director"]["detail"] != "forged"



# --- SYRD-48: the release phase is derived from what is actually deployed -----


def _origin_backed_source(tmp: Path) -> tuple[Path, str]:
    """A real source checkout, and the sha its `origin/main` resolves to."""
    _origin, repo = _make_origin_backed_repo(tmp)
    return repo, _run_git(["git", "rev-parse", "origin/main"], cwd=repo).stdout.strip()


def _advance_origin(repo: Path) -> str:
    """Publish a newer release on the pinned ref, as a later Switchyard would."""
    (repo / "tracked.txt").write_text("newer\n", encoding="utf-8")
    _run_git(["git", "add", "tracked.txt"], cwd=repo)
    _run_git(["git", "commit", "-m", "newer release"], cwd=repo)
    _run_git(["git", "push", "origin", "HEAD:main"], cwd=repo)
    _run_git(["git", "fetch", "origin"], cwd=repo)
    return _run_git(["git", "rev-parse", "origin/main"], cwd=repo).stdout.strip()


def _deploying_runner(inner, *, board_root: Path, deploys: list[str], deploy: bool = True):
    """The fake host, with the two things a release actually depends on made real.

    Resolving the pinned ref is a read of a real repository, and the deploy moves
    `current`. A fake that answers both from nowhere would let this suite pass
    while reporting a release nothing deployed (SYRD-48).
    """

    def call(args, **kwargs):
        argv = [str(part) for part in args]
        # Root's own git reads are de-escalated to the owning account, so the
        # read to answer arrives wrapped in sudo (SYRD-48).
        bare = argv
        if bare[:2] == ["sudo", "-u"] and len(bare) > 3:
            bare = bare[4:] if bare[3] == "-H" else bare[3:]
        if bare[:1] == ["git"] and ("ls-remote" in bare or "rev-parse" in bare):
            passthrough = dict(kwargs)
            passthrough.setdefault("text", True)
            passthrough.setdefault("stdout", subprocess.PIPE)
            passthrough.setdefault("stderr", subprocess.PIPE)
            return subprocess.run(bare, **passthrough)
        if "deploy-restart" in " ".join(argv):
            deploys.append(" ".join(argv))
            if deploy:
                sha = _run_git(
                    ["git", "rev-parse", "origin/main"], cwd=_deploying_runner.source
                ).stdout.strip()
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


def _tenant_before_cutover(tmp: Path) -> tuple[Path, Path, Path, set[str]]:
    """A legacy tenant, its source checkout, its board root, and its role accounts."""
    source_repo, _target = _origin_backed_source(tmp)
    _deploying_runner.source = source_repo
    board_root = _deployed_release(tmp, "porter", "1" * 40)
    config_path, _ = _declarative_tenant(tmp, board_root=board_root)
    roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
    return config_path, source_repo, board_root, {f"porter-{role}" for role in roles}


def _cut_over(config_path, source_repo, board_root, accounts, *, deploy: bool = True):
    """Run the invocation that performs the transaction. Returns its output."""
    deploys: list[str] = []
    with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
        _result, output, _m = _upgrade(
            config_path,
            as_root=True,
            exists=accounts,
            runner=_deploying_runner(
                tenant.runner(), board_root=board_root, deploys=deploys, deploy=deploy
            ),
            board=_board_with_marker(False),
            source_repo=source_repo,
        )
    return output, deploys


def test_the_release_the_transaction_deployed_is_reported_done_not_owed() -> None:
    """The transaction switches the release, so no operator deploy is left."""
    with tempfile.TemporaryDirectory(prefix="release-phase-done.") as tmp:
        config_path, source_repo, board_root, accounts = _tenant_before_cutover(Path(tmp))
        target = _run_git(["git", "rev-parse", "origin/main"], cwd=source_repo).stdout.strip()

        output, deploys = _cut_over(config_path, source_repo, board_root, accounts)

        assert "running under per-role identities" in output, output
        # The deploy happened once, inside the transaction, and moved the pointer.
        assert len(deploys) == 1, deploys
        assert (board_root / "current").resolve().name == target
        assert "release unchanged; no release deploy needed" in output, output
        assert "board release is deployed and no further deploy is needed" in output, output
        # The sentence that sent an operator looking for a deploy that must not happen.
        assert "after that deploy" not in output, output
        assert "deployment sequence" not in output, output
        # The phase says so too, in root's own record.
        config = team_launcher.load_project_config("porter", config_path)
        trusted = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert team_launcher.upgrade_phase_state(trusted, "release") == "done", trusted
        assert "identities transaction switched it" in trusted["phases"]["release"]["detail"]
        assert any(
            line.split()[:3] == ["release", "operator", "done"] for line in output.splitlines()
        ), output
        # The director's step is still named, and is still the director's.
        assert "The remaining step is the director's" in output, output
        assert "finish-upgrade porter" in output, output
        assert "root cannot make that write" in output, output


def test_a_release_that_really_moved_on_is_still_owed_and_still_printed() -> None:
    """Deriving the phase must not stop a genuine later release being offered."""
    with tempfile.TemporaryDirectory(prefix="release-phase-ready.") as tmp:
        config_path, source_repo, board_root, accounts = _tenant_before_cutover(Path(tmp))
        _cut_over(config_path, source_repo, board_root, accounts)
        deployed = (board_root / "current").resolve().name
        # A later release is published. The tenant is already cut over, so no
        # transaction runs and the deploy is genuinely an operator's to make.
        newer = _advance_origin(source_repo)
        assert newer != deployed

        deploys: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, output, _m = _upgrade(
                config_path, as_root=True, exists=accounts,
                runner=_deploying_runner(
                    tenant.runner(), board_root=board_root, deploys=deploys, deploy=False
                ),
                board=_board_with_marker(False), source_repo=source_repo,
            )

        assert deploys == [], deploys
        assert f"old: {deployed}" in output, output
        assert f"new: {newer}" in output, output
        assert "no release deploy needed" not in output, output
        assert "no further deploy is needed" not in output, output
        assert "after that deploy" in output, output
        config = team_launcher.load_project_config("porter", config_path)
        trusted = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert team_launcher.upgrade_phase_state(trusted, "release") == "ready", trusted


def test_a_dry_run_reports_the_deployed_release_and_records_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="release-phase-dry.") as tmp:
        config_path, source_repo, board_root, accounts = _tenant_before_cutover(Path(tmp))
        _cut_over(config_path, source_repo, board_root, accounts)
        config = team_launcher.load_project_config("porter", config_path)
        journal_path = team_launcher.privileged_upgrade_journal_path(config)
        before_journal = journal_path.read_bytes()
        before_config = config_path.read_bytes()

        deploys: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, output, _m = _upgrade(
                config_path, as_root=True, exists=accounts, dry_run=True,
                runner=_deploying_runner(
                    tenant.runner(), board_root=board_root, deploys=deploys, deploy=False
                ),
                board=_board_with_marker(False), source_repo=source_repo,
            )

        assert deploys == [], deploys
        assert "board release is deployed and no further deploy is needed" in output, output
        assert config_path.read_bytes() == before_config
        assert journal_path.read_bytes() == before_journal


def test_the_legacy_to_cutover_to_director_sequence_names_one_remaining_step() -> None:
    """Legacy tenant, operator accounts, transaction, director -- and no second deploy."""
    with tempfile.TemporaryDirectory(prefix="release-phase-sequence.") as tmp:
        config_path, source_repo, board_root, accounts = _tenant_before_cutover(Path(tmp))

        # 1. Legacy: the accounts do not exist, so nothing is deployed and no
        #    deploy is offered.
        deploys: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, first, _m = _upgrade(
                config_path, as_root=True, exists=set(),
                runner=_deploying_runner(tenant.runner(), board_root=board_root, deploys=deploys),
                board=_board_with_marker(False), source_repo=source_repo,
            )
        assert "still share the project account" in first, first
        assert "withholding" in first, first
        assert deploys == [], deploys
        assert "no further deploy is needed" not in first, first

        # 2. The operator creates the accounts; the transaction switches the
        #    release itself, and the output names only the director's step.
        second, deploys = _cut_over(config_path, source_repo, board_root, accounts)
        assert "running under per-role identities" in second, second
        assert len(deploys) == 1, deploys
        assert "no further deploy is needed" in second, second
        assert "deployment sequence" not in second, second

        # 3. The director's own write, from the director's own account, and the
        #    release it reports is the one already running.
        config = team_launcher.load_project_config("porter", config_path)
        printed: list[str] = []
        original_migrate = team_launcher.migrate_declarative_director_onboarding
        original_opener = team_launcher._open_board_url
        original_exists = team_launcher.local_account_exists
        try:
            team_launcher.migrate_declarative_director_onboarding = (
                lambda config, **kwargs: _mark_projection_migrated(config_path) or True
            )
            team_launcher._open_board_url = _board_with_marker(True)
            team_launcher.local_account_exists = lambda account: account in accounts
            with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                assert team_launcher.finish_upgrade_command(
                    config, config_path=config_path, source_repo=source_repo,
                    runner=_deploying_runner(
                        tenant.runner(), board_root=board_root, deploys=[], deploy=False
                    ),
                    print_func=printed.append,
                ) == 0, printed
        finally:
            team_launcher.migrate_declarative_director_onboarding = original_migrate
            team_launcher._open_board_url = original_opener
            team_launcher.local_account_exists = original_exists
        finished = "\n".join(printed)
        assert "no release deploy needed" in finished, finished
        assert "deployment sequence" not in finished, finished
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "director") == "done", journal
        assert team_launcher.upgrade_phase_state(journal, "release") == "done", journal


def test_the_upgrade_documentation_describes_the_flow_that_exists() -> None:
    """The docs carry this to operators and to the director, and had outlived it."""
    launcher_doc = (ROOT / "docs" / "team-launcher.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "onboarding" / "switchyard-director-guide.md").read_text(encoding="utf-8")

    # There is no compatibility phase, and no deploy is printed ahead of the director.
    assert {phase for phase, _owner, _detail in team_launcher.UPGRADE_PHASES} == {
        "artifacts", "accounts", "identities", "release", "director",
    }
    assert "`compatibility` phase" not in launcher_doc
    assert "no separate compatibility deploy" in launcher_doc
    assert "The release phase is derived, not assumed." in launcher_doc
    # The duplicated release-pointer phrase.
    assert launcher_doc.count("the release pointer and") == 1, launcher_doc
    # The director is told, in the onboarding packet, what is actually left.
    assert "there is no second deploy for an operator to run" in guide
    assert "as the only\nremaining step" in guide


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_upgrade_cutover_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
