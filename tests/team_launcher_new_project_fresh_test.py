#!/usr/bin/env python3
"""A fresh project's provisioning and launch, run as the root they are on a host (SYRD-69, SYRD-607, SYRD-759).

Moved out of team_launcher_new_project_test unchanged in intent (SYRD-808), with the fixtures they had lost: root
writes by path only into a directory it alone controls, executes only its own staged copy, and acts for an owner whose
home is the case's own.

Guard-only (SYRD-808): `host_state_guard` refuses any read or exec of this host's Switchyard state and privileged
tools, and the last check fails on any reach. Run it as an ordinary account, never as root, and never in a sweep
that runs as root.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import host_state_guard  # noqa: E402  - before any production module: no host state (SYRD-808)
host_state_guard.install()
from unittest.mock import patch

from team_launcher_test_helpers import *
from owner_home_sandbox import owner_home_stood_in
from root_tree_stand_in import root_alone, roots_own


def test_fresh_provisioning_emits_no_role_account_handoff() -> None:
    """SYRD-69: provisioning ends with the project account and launch gate."""
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="switchyard-handoff.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        printed: list[str] = []
        # SYRD-62: the handoff is published where only root can have written it,
        # so provisioning is driven as the root it is on a host.
        privileged_root = tmp_path / "etc-switchyard"

        # The fixture source stands in for root's installed release (SYRD-630). Root's dry run refuses an output
        # directory in the owner's tree (SYRD-666), and this suite's stand-in home for the owner holds every
        # temporary directory, so the owner's home is one of this case's own for that judgement.
        from scripts import owner_writes

        real_owner_home = owner_writes.owner_home
        owner_writes.owner_home = lambda _owner: str(tmp_path / "owner-home")
        # Root writes the artifacts by path into a directory only it controls (SYRD-759): the case's own, here.
        with _provisioning_as_root(privileged_root), roots_own(source_repo), root_alone(tmp_path), \
                redirect_stdout(StringIO()):
            assert new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                commit_git_dir="/srv/git/review-cache.git",
                repository=project_repo,
                output_dir=output_dir,
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                print_func=printed.append,
            ) == 0
        owner_writes.owner_home = real_owner_home

        handoff = privileged_root / "porter" / "porter-role-accounts.sh"
        assert not handoff.exists(), printed
        assert not (output_dir / "porter-role-accounts.sh").exists(), sorted(
            path.name for path in output_dir.iterdir()
        )


def test_fresh_project_launches_without_a_role_account_handoff() -> None:
    """SYRD-69: the project account can pass directly into the launch gate."""
    from scripts import presentation_controller

    class NewProjectRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"] and args[-1]:
                target = Path(args[-1])
                if str(target).startswith(tempfile.gettempdir()):
                    target.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    polled: list[object] = []
    printed: list[str] = []
    original_report = team_launcher.report_launch_session_records
    original_authority_probe = team_launcher.process_authority_board_compatibility
    original_runtime_config = presentation_controller.runtime_assignment_config
    try:
        team_launcher.report_launch_session_records = lambda *a, **kw: polled.append(kw) or []
        team_launcher.process_authority_board_compatibility = lambda _config: (
            True, "simulated provisioned process-authority board"
        )
        presentation_controller.runtime_assignment_config = lambda config, **_kwargs: config
        with tempfile.TemporaryDirectory(prefix="switchyard-deferred-launch.") as tmp:
            tmp_path = Path(tmp)
            source_repo = tmp_path / "source-repo"
            source_repo.mkdir()
            # SYRD-62: the handoff it prints is root's published copy, so root's directory goes in the sandbox.
            # --execute runs root's own staged copy, so it is decided by the process's euid, not euid_getter
            # (SYRD-607): the case runs as the root it is on a host, root's trees redirected, for a tenant owned by the
            # account running it, whose files root's chowns then leave as they were -- never `otto-agent`, a real
            # account of this host (SYRD-808).
            privileged_root = tmp_path / "etc-switchyard"
            owner = team_launcher.current_user_name()
            # The owner's home here, where `switchyard new` puts its project (home_base / owner) and root walks down from;
            # root reads the generated configuration for the owner from inside it, so the output goes there too.
            owner_home = tmp_path / "home" / owner
            owner_home.mkdir(parents=True)
            # The launch's report looks for windows root opened among every process; here, among none of the host's.
            proc_root = tmp_path / "proc"
            proc_root.mkdir()
            real_fchown = os.fchown

            def fchown_as_root(fd: int, uid: int, gid: int) -> None:
                # The case is not root: handing a staged artifact to root is the one thing it cannot do, so that is
                # dropped, as _staging_sudo_shim drops it; root's staging itself is root_runs_its_own_copy_test's.
                if (uid, gid) != (0, 0):
                    real_fchown(fd, uid, gid)

            with _provisioning_as_root(privileged_root), roots_own(source_repo), root_alone(tmp_path), \
                    patch.object(os, "fchown", fchown_as_root), owner_home_stood_in(owner, owner_home), \
                    patch.object(team_launcher, "ensure_staged_role_tooling", lambda *_a, **_k: []), \
                    patch.object(team_launcher, "run_first_run_auth_phase",
                                 lambda *_a, **_k: team_launcher.FirstRunAuthReport({}, [])), \
                    patch.object(team_launcher, "_new_project_control_repository", lambda project, _owner: (
                        owner_home / ".local/state/switchyard/projects" / project / "control.git")), \
                    patch.object(team_launcher, "_new_project_worktree_base",
                                 lambda project, _owner: owner_home / f"{project}-worktrees"), \
                    patch.object(team_launcher, "PROC_ROOT", proc_root), \
                    redirect_stdout(StringIO()):
                # The role bundle a launch stages is single_owner_staged_tooling_test's, and the owner's provider
                # sign-in and first run team_launcher_first_run_auth_test's; here both stand finished. The
                # control repository's and worktrees' paths are spelled `/home/<owner>` (new_project_artifacts,
                # team_launcher), not the owner's home, so they follow the home stood in here.
                assert (
                    switchyard_new_command(
                        desktop_policy=Path("headless"),
                        slug="porter",
                        agent_name=owner,
                        project_name="Porter System",
                        source_repo=source_repo,
                        output_dir=owner_home / "out",
                        role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                        yes=True,
                        allow_existing_owner_user=True,
                        home_base=tmp_path / "home",
                        euid_getter=lambda: 0,
                        runner=NewProjectRunner(),
                        input_func=lambda _prompt: "",
                        port_in_use=lambda _port: False,
                        socket_exists=lambda _path: False,
                        registry_dir=tmp_path / "registry",
                        konsole_process_launcher=RecordingProcessLauncher(),
                        print_func=printed.append,
                    )
                    == 0
                ), printed
    finally:
        team_launcher.report_launch_session_records = original_report
        team_launcher.process_authority_board_compatibility = original_authority_probe
        presentation_controller.runtime_assignment_config = original_runtime_config
        os.environ.pop("SWITCHYARD_PRIVILEGED_PROVISION_ROOT", None)

    assert not any("were not started" in line for line in printed), printed
    assert not any("porter-role-accounts.sh" in line for line in printed), printed
    # The launch is reported as one. Which words depend on the layout this
    # environment resolves to: a layout that opens its own window says so, and
    # the viewer -- which root cannot show -- says every pane is up and where the
    # window opens (SYRD-221 UAT, test10).
    assert any(
        "full pane window started for porter" in line or "every pane of porter is up" in line
        for line in printed
    ), printed
    assert len(polled) == 1, polled
    assert any("design" in line for line in printed), printed


def main() -> int:
    try:
        run_team_launcher_tests(globals(), first=())
    finally:  # a reach that also broke a case is reported as the reach
        host_state_guard.assert_untouched("team_launcher_new_project_fresh_test")
    print("team_launcher_new_project_fresh_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
