#!/usr/bin/env python3
"""A tenant upgrade must be ordered, journaled and identity-safe.

The current contract repatriates stopped legacy role state to the single
project account, refuses before mutation while any old role pane is live, and
keeps release activation and director-authority work as separately owned
phases.
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
            if argv[:2] == ["git", "-C"] and argv[3:5] == ["rev-parse", "--verify"]:
                # Resolving which commit is being installed is a real question
                # about a real repository; answering it with a stub would make
                # the release verification untestable here.
                return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if argv[:1] == ["install"] and "-d" in argv:
                # The directory form names no source; modelling it as a copy
                # made the last flag value look like one.
                Path(argv[-1]).mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(argv, 0, "", "")
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

        # The transaction stops the workers and leaves the presentation alone,
        # so it is the worker-only stop that has to be intercepted here; the
        # whole-project stop is kept patched so a test that reached it would
        # be visible rather than silently opening a window (SYRD-65).
        self._stop_workers = team_launcher.stop_role_sessions
        team_launcher.stop_role_sessions = _stop
        team_launcher.launch_project = _launch
        team_launcher.stop_project = _stop
        return self

    def __exit__(self, *_exc):
        team_launcher.uid_for_user = self._uid
        team_launcher.launch_project = self._launch
        team_launcher.stop_project = self._stop
        team_launcher.local_account_exists = self._exists
        team_launcher._start_role_sessions_without_a_window = self._start_sessions
        team_launcher.stop_role_sessions = self._stop_workers
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
        # The canary too: the deploy starts it through systemd, and the
        # transaction installs the same three units the printed operator
        # sequence does (SYRD-63).
        f"{project}-ticket-board-canary.service",
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


#: The release these fixtures stage their privileged tooling from.
TRUSTED_RELEASE_ROOT: Path | None = None


def trusted_release_root() -> Path | None:
    """The release the last tenant staged from.

    A function rather than the global, because a suite that imports the name
    binds it once at import time and would get the value from before any tenant
    was built (SYRD-97 review).
    """
    return TRUSTED_RELEASE_ROOT


def _declarative_tenant(
    tmp: Path, *, project: str = "porter", accounts: bool = False, board_root: Path | None = None
) -> tuple[Path, Path]:
    _privileged_root(tmp)
    # Privileged tooling may only be staged from a verified release, so these
    # tenants have one to be staged from (SYRD-97 review).
    global TRUSTED_RELEASE_ROOT
    TRUSTED_RELEASE_ROOT = trusted_release_root_for(stage_trusted_releases())
    _stage_units(project)
    # The upgrade stages and verifies this tenant's role tooling before it moves
    # any role, so the sandbox holds a real bundle (SYRD-62).
    # From the verified release, because that is where the upgrade will stage
    # from and the bundle is checked against it (SYRD-97 review).
    _stage_role_tooling(tmp, project, release_root=TRUSTED_RELEASE_ROOT)
    config_path = _write_six_visible_role_config(tmp, project=project)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    # A real account: the privileged paths chown generated files to the project
    # owner, and this suite runs the root branch.
    payload["run_as_user"] = team_launcher.current_user_name()
    # A real workflow document declares each role's capabilities, and the
    # control role is the one holding them -- not the one named "director"
    # (SYRD-49). This tenant's control role keeps that name; a tenant whose
    # control role is named something else is covered by the role path access
    # regression.
    payload["workflow"] = {
        "roles": [
            {
                "name": role["role"],
                "active": True,
                "capabilities": (
                    ["add_comment", "set_manually_controlled", "merge"]
                    if role["role"] == "director"
                    else ["add_comment"]
                ),
            }
            for role in payload["roles"]
        ]
    }
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
    # None is "said nothing about the ref", which is what these cases mean and
    # what the CLI now passes when the option is absent (SYRD-61).
    deploy_ref: str | None = None,
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
            tooling_root=config_path.parent / "tooling",
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
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        board_without_marker = _board_with_marker(False)
        before = json.loads(config_path.read_text(encoding="utf-8"))["roles"]

        # 1. A live legacy role is a hard pre-mutation stop. The operator must
        #    checkpoint it; creating more accounts is no longer a phase.
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, first, _m = _upgrade(
                config_path, as_root=True, exists=set(), runner=tenant.runner(),
                board=board_without_marker,
            )
        assert result == 1, first
        assert "stop it at a resumable checkpoint before repatriation" in first, first
        assert json.loads(config_path.read_text(encoding="utf-8"))["roles"] == before
        assert "deploy-restart" not in first, first

        # 2. Once checkpointed, the next invocation repatriates state and
        #    removes every dedicated-account binding. The accounts themselves
        #    are deliberately left intact.
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            tenant.live = False
            result, second, _m = _upgrade(
                config_path, as_root=True, exists=set(), runner=tenant.runner(),
                board=board_without_marker,
            )
        assert result == 0, second
        assert "repatriated porter's resumable role state" in second, second
        assert "dedicated accounts were left intact" in second, second
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert after["role_state_isolation"] is True, after
        assert all("run_as_user" not in role for role in after["roles"]), after
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
    """Stale role bindings do not block panes already served by the owner."""
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
        assert "repatriated porter's resumable role state" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert after["role_state_isolation"] is True
        assert all("run_as_user" not in role for role in after["roles"])


def test_the_observed_partial_state_is_detected_and_reverted() -> None:
    """Stopped legacy bindings are removed even when their accounts are absent."""
    with tempfile.TemporaryDirectory(prefix="upgrade-partial.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        before = json.loads(config_path.read_text(encoding="utf-8"))
        assert all(role.get("run_as_user") for role in before["roles"])

        result, output, _migrations = _upgrade(config_path, as_root=True, exists=set())
        assert result == 0, output
        assert "repatriated porter's resumable role state" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert after["role_state_isolation"] is True, after
        assert all("run_as_user" not in role for role in after["roles"]), after
        config = team_launcher.load_project_config("porter", config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") == "done", journal


def test_a_freshly_provisioned_project_is_left_as_provisioned() -> None:
    """A stopped legacy tenant is normalized without creating accounts."""
    with tempfile.TemporaryDirectory(prefix="upgrade-fresh.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        result, output, _m = _upgrade(config_path, as_root=True, exists=set(), runner=FakeRunner())
        assert result == 0, output
        assert "repatriated porter's resumable role state" in output, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert after["role_state_isolation"] is True, after
        assert all("run_as_user" not in role for role in after["roles"]), after
        assert not team_launcher.trusted_role_account_migration_path(
            team_launcher.load_project_config("porter", config_path)
        ).exists()


def test_the_cutover_happens_only_once_every_account_exists() -> None:
    with tempfile.TemporaryDirectory(prefix="upgrade-cutover.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        result, output, _m = _upgrade(config_path, as_root=True, exists=set())
        assert result == 0, output
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert after["role_state_isolation"] is True, after
        assert all("run_as_user" not in role for role in after["roles"]), after

        first = config_path.read_bytes()
        result, second, _m = _upgrade(config_path, as_root=True, exists=set())
        assert result == 0, second
        assert config_path.read_bytes() == first
        assert "repatriated porter's resumable role state" not in second, second


def test_a_cutover_whose_processes_keep_the_old_uid_is_rolled_back() -> None:
    """A live legacy pane prevents repatriation before any mutation."""
    with tempfile.TemporaryDirectory(prefix="upgrade-rollback.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        before = json.loads(config_path.read_text(encoding="utf-8"))["roles"]
        with _RunningTenant(config_path, account_uid=os.getuid() + 4242) as tenant:
            result, output, _m = _upgrade(
                config_path, as_root=True, exists=set(), runner=tenant.runner()
            )
        assert result == 1
        assert "stop it at a resumable checkpoint before repatriation" in output, output
        assert json.loads(config_path.read_text(encoding="utf-8"))["roles"] == before
        assert tenant.stops == [] and tenant.launches == []


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
                    source_repo=TRUSTED_RELEASE_ROOT,
                    tooling_dir=config_path.parent / "tooling" / "porter",
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
                        source_repo=TRUSTED_RELEASE_ROOT,
                        tooling_dir=config_path.parent / "tooling" / "porter",
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
                source_repo=TRUSTED_RELEASE_ROOT,
                tooling_dir=config_path.parent / "tooling" / "porter",
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
                source_repo=TRUSTED_RELEASE_ROOT,
                tooling_dir=config_path.parent / "tooling" / "porter",
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
                    source_repo=TRUSTED_RELEASE_ROOT,
                    tooling_dir=config_path.parent / "tooling" / "porter",
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
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        # A live legacy pane blocks before the release phase can be offered.
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            result, output, _m = _upgrade(
                config_path, as_root=True, exists=set(), runner=tenant.runner()
            )
        assert result == 1, output
        assert "checkpoint before repatriation" in output, output
        assert "deploy-restart" not in output, output

        # A second legacy tenant with live panes is refused the same way,
        # regardless of which retired accounts happen to exist.
        partial_path, _ = _declarative_tenant(Path(tmp) / "partial", accounts=True)
        result, output, _m = _upgrade(
            partial_path, as_root=True, exists={"porter-designer"}, runner=_live_runner(partial_path)
        )
        assert result == 1, output
        assert "checkpoint before repatriation" in output, output
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
        assert "finish-upgrade porter" in output, output
        trusted = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert team_launcher.upgrade_phase_state(trusted, "director") != "done", trusted
        # Root's record is a different file in a directory the tenant does not own.
        root_record = team_launcher.privileged_upgrade_journal_path(config)
        assert root_record.is_file(), root_record
        assert root_record != team_launcher.upgrade_journal_path(config, config_path=config_path)
        assert json.loads(root_record.read_text())["phases"]["director"]["detail"] != "forged"



# --- SYRD-48: the release phase is derived from what is actually deployed -----


def _origin_backed_source(tmp: Path) -> tuple[Path, str]:
    """A real source checkout, and the sha its `origin/main` resolves to.

    Its release is staged here rather than at each caller: privileged tooling may
    only be staged from a verified release, and this repository's commits are the
    ones an upgrade pinned at it will select (SYRD-97 review).
    """
    _origin, repo = _make_origin_backed_repo(tmp)
    # Shaped like the thing it stands in for. An upgrade pinned at this checkout
    # stages its privileged tooling from the release built out of this commit, so
    # a repository holding only a text file cannot stand for a Switchyard source
    # -- there would be nothing to stage and nothing to verify (SYRD-97 review).
    for name in ("scripts", "skills"):
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, repo / name, dirs_exist_ok=True)
    _run_git(["git", "add", "scripts", "skills"], cwd=repo)
    _run_git(["git", "commit", "-m", "Switchyard source"], cwd=repo)
    _run_git(["git", "push", "origin", "HEAD:main"], cwd=repo)
    _run_git(["git", "fetch", "origin"], cwd=repo)
    sha = _run_git(["git", "rev-parse", "origin/main"], cwd=repo).stdout.strip()
    stage_trusted_release(repo, ref=sha)
    return repo, sha


def _advance_origin(repo: Path, tmp: Path | None = None) -> str:
    """Publish a newer release on the pinned ref, as a later Switchyard would."""
    (repo / "tracked.txt").write_text("newer\n", encoding="utf-8")
    _run_git(["git", "add", "tracked.txt"], cwd=repo)
    _run_git(["git", "commit", "-m", "newer release"], cwd=repo)
    _run_git(["git", "push", "origin", "HEAD:main"], cwd=repo)
    _run_git(["git", "fetch", "origin"], cwd=repo)
    sha = _run_git(["git", "rev-parse", "origin/main"], cwd=repo).stdout.strip()
    # The newer release has to be stageable too, or the upgrade that is supposed
    # to move onto it has nothing verified to move onto -- and the tenant's
    # staged bundle has to come from it, because that is what the transaction
    # checks the bundle against (SYRD-97 review).
    stage_trusted_release(repo, ref=sha)
    if tmp is not None:
        _stage_role_tooling(
            tmp, "porter", release_root=TEST_SWITCHYARD_SHARED_INSTALL_ROOT / "releases" / sha
        )
    return sha


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
    source_repo, target = _origin_backed_source(tmp)
    _deploying_runner.source = source_repo
    board_root = _deployed_release(tmp, "porter", "1" * 40)
    config_path, _ = _declarative_tenant(tmp, board_root=board_root)
    # This tenant's upgrade is pinned at its own checkout, so its staged bundle
    # has to come from that checkout's release rather than from the one
    # `_declarative_tenant` staged for the default ref: the transaction checks
    # the bundle against whichever release it was pinned at (SYRD-97 review).
    _stage_role_tooling(
        tmp, "porter", release_root=TEST_SWITCHYARD_SHARED_INSTALL_ROOT / "releases" / target
    )
    roles = [role["role"] for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"]]
    return config_path, source_repo, board_root, {f"porter-{role}" for role in roles}


def _cut_over(config_path, source_repo, board_root, accounts, *, deploy: bool = True):
    """Repatriate a checkpointed tenant and report the separate release step."""
    deploys: list[str] = []
    with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
        tenant.live = False
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
    """Repatriation never folds the separately owned release step into itself."""
    with tempfile.TemporaryDirectory(prefix="release-phase-done.") as tmp:
        config_path, source_repo, board_root, accounts = _tenant_before_cutover(Path(tmp))
        target = _run_git(["git", "rev-parse", "origin/main"], cwd=source_repo).stdout.strip()

        output, deploys = _cut_over(config_path, source_repo, board_root, accounts)

        assert "repatriated porter's resumable role state" in output, output
        assert deploys == [], deploys
        assert (board_root / "current").resolve().name == "1" * 40
        assert target in output, output
        assert "matching-release deployment sequence" in output, output
        assert "after that deploy" in output, output
        config = team_launcher.load_project_config("porter", config_path)
        trusted = team_launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
        assert team_launcher.upgrade_phase_state(trusted, "release") == "ready", trusted
        assert any(
            line.split()[:3] == ["release", "operator", "ready"] for line in output.splitlines()
        ), output
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
        newer = _advance_origin(source_repo, Path(tmp))
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
        assert "matching-release deployment sequence" in output, output
        assert config_path.read_bytes() == before_config
        assert journal_path.read_bytes() == before_journal


def test_the_legacy_to_cutover_to_director_sequence_names_one_remaining_step() -> None:
    """Live refusal, checkpointed repatriation, then the director-owned phase."""
    with tempfile.TemporaryDirectory(prefix="release-phase-sequence.") as tmp:
        config_path, source_repo, board_root, accounts = _tenant_before_cutover(Path(tmp))

        # 1. Live legacy panes stop the migration before any release action.
        deploys: list[str] = []
        with _RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            _result, first, _m = _upgrade(
                config_path, as_root=True, exists=set(),
                runner=_deploying_runner(tenant.runner(), board_root=board_root, deploys=deploys),
                board=_board_with_marker(False), source_repo=source_repo,
            )
        assert "checkpoint before repatriation" in first, first
        assert deploys == [], deploys
        assert "no further deploy is needed" not in first, first

        # 2. After checkpointing, state is repatriated and the release remains
        #    an explicit operator-owned phase.
        second, deploys = _cut_over(config_path, source_repo, board_root, accounts)
        assert "repatriated porter's resumable role state" in second, second
        assert deploys == [], deploys
        assert "matching-release deployment sequence" in second, second
        assert "after that deploy" in second, second

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
        assert "matching-release deployment sequence" in finished, finished
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "director") == "done", journal
        assert team_launcher.upgrade_phase_state(journal, "release") == "ready", journal


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


def _artifact_commands(artifact: str) -> list[str]:
    """The lines an operator's shell actually runs, not the prose around them."""
    return [
        line.strip()
        for line in artifact.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_operator_artifact_describes_the_sequence_that_exists() -> None:
    """The generated hand-off still carried the pre-transaction flow (SYRD-48)."""
    with tempfile.TemporaryDirectory(prefix="role-account-artifact.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        config = team_launcher.load_project_config("porter", config_path)
        artifact = team_launcher.render_role_account_migration(config)
        commands = _artifact_commands(artifact)

        # It hands back to the ordered driver, and that is the last thing it runs.
        assert "sudo switchyard upgrade porter" in commands, commands
        assert commands[-1] == "sudo switchyard upgrade porter", commands[-5:]
        # The transaction restarts the board itself; a second restart bounces a
        # board that is already serving the roles it just verified.
        assert not any("systemctl restart" in command for command in commands), commands
        assert "porter-ticket-board.service" not in artifact, artifact
        # The director's write is named, but never executed from this script: it
        # is authorized from the director's own uid.
        assert not any("finish-upgrade" in command for command in commands), commands
        assert "switchyard finish-upgrade porter" in artifact, artifact
        assert "in the director's own session" in artifact, artifact
        # And it is named after the upgrade, not before the uid move.
        assert artifact.index("sudo switchyard upgrade porter") < artifact.index(
            "switchyard finish-upgrade porter"
        ), artifact
        assert "must not happen before the" not in artifact, artifact
        assert "director has made its own board write" not in artifact, artifact
        # Nothing else about the artifact changed: it still creates the accounts
        # and still refuses to move a worktree.
        for role in config.roles:
            account = f"porter-{role.role}"
            assert f"if ! getent passwd '{account}'" in artifact, role.role
            # By the quoted path, not by substring: a role whose workdir is the
            # project repository is a prefix of the provisioning directory the
            # director is granted, and a bare `in` reads that as a move of the
            # worktree (SYRD-49).
            assert f"'{role.workdir}'" not in artifact, role.role
        assert "sudo switchyard seed-role-credentials porter" in commands, commands


def test_the_upgrade_writes_that_artifact_when_the_accounts_are_missing() -> None:
    """Project-account migration never publishes an account-creation artifact."""
    with tempfile.TemporaryDirectory(prefix="role-account-artifact-written.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        _result, output, _m = _upgrade(config_path, as_root=True, exists=set())
        config = team_launcher.load_project_config("porter", config_path)
        assert "repatriated porter's resumable role state" in output, output
        assert not team_launcher.trusted_role_account_migration_path(config).exists()
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        assert payload["role_state_isolation"] is True
        assert all("run_as_user" not in role for role in payload["roles"])


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_upgrade_cutover_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
