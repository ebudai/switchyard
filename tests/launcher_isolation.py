"""SYRD-812: what keeps a launcher suite's launches off the invoking account's live state, and two fixtures they need.

Moved out of team_launcher_test_helpers, which re-exports them: that file is over the size its policy allows to grow.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import scripts.team_launcher as team_launcher

ROOT = Path(__file__).resolve().parents[1]
#: Each GitHub identity probe the stand-in answered instead of running, and what it answered (Audit of 48499ffe: the
#: stand-in was pinned by nothing, and without it the upgrade cases authenticate to GitHub with the invoker's keys).
GITHUB_PROBES_ANSWERED: list[list[str]] = []
OFFLINE_GITHUB_ANSWER = "no GitHub probe in tests"


@contextlib.contextmanager
def isolated_launches(LIVE_PANE_SESSION_PATHS):
    """For the whole run of a launcher suite: records bound for a live session store go to a mirror, the default
    store's legacy seeding is skipped when its target is live, and no GitHub identity probe leaves the host."""
    # SYRD-812: a launch writes into each role's session store what its runtime started against (SYRD-191,
    # `<role>.provider-state.json`) and why it resumed or not (`<pane>.resume.log`) -- for a fixture config with no
    # session_dir, the live default store. Since 6ad96d9c these suites wrote into the running account's own
    # pane-sessions, and the snapshot guard below then replaced each suite's first real failure. A record bound for a
    # live store goes to a mirror instead; reads take the same path, so a launch still finds what it recorded.
    from scripts import provider_runtime_state, resume_history

    mirror = Path(tempfile.mkdtemp(prefix="syrd812-session-records."))

    # The live stores: those the environment names, and the invoking account's own, which a fixture owned by that
    # account resolves from its passwd home whatever HOME says.
    import pwd as _pwd

    live_stores = [*LIVE_PANE_SESSION_PATHS,
                   Path(_pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"]

    def mirrored(path: Path) -> Path:
        for index, live in enumerate(live_stores):
            try:
                return mirror / str(index) / path.expanduser().relative_to(live.expanduser())
            except ValueError:
                continue
        return path

    original_record_path = provider_runtime_state._provider_state_record_path
    original_history_path = resume_history.resume_history_path
    provider_runtime_state._provider_state_record_path = lambda config, role: mirrored(original_record_path(config, role))
    resume_history.resume_history_path = lambda target, session_dir: mirrored(original_history_path(target, session_dir))
    # SYRD-812: and no launcher suite reaches GitHub. An upgrade tries the owner's GitHub identity with
    # `ssh -T git@github.com` through whatever runner its caller holds -- the real one, in these suites -- and ssh
    # reads the invoking account's own keys, whatever HOME says. A probe on the real runner is answered here instead;
    # a suite that hands it a runner of its own keeps it.
    original_github_status = team_launcher.github_identity_status

    def offline_github_status(owner_user, owner_home, *, runner=subprocess.run, **kwargs):
        if runner is subprocess.run:
            def runner(args, **_kw):
                GITHUB_PROBES_ANSWERED.append([str(arg) for arg in args])  # what a suite checks was never run
                return subprocess.CompletedProcess(args, 255, OFFLINE_GITHUB_ANSWER, "")
        return original_github_status(owner_user, owner_home, runner=runner, **kwargs)

    team_launcher.github_identity_status = offline_github_status
    # ... and its seeding of the default store from legacy ones creates that live directory and may copy into it.
    original_seed = team_launcher.seed_default_session_dir_from_legacy_sources
    team_launcher.seed_default_session_dir_from_legacy_sources = (
        lambda session_dir: [] if mirrored(Path(session_dir)) != Path(session_dir) else original_seed(session_dir))
    try:
        yield
    finally:
        provider_runtime_state._provider_state_record_path = original_record_path
        resume_history.resume_history_path = original_history_path
        team_launcher.seed_default_session_dir_from_legacy_sources = original_seed
        team_launcher.github_identity_status = original_github_status
        shutil.rmtree(mirror, ignore_errors=True)


@contextlib.contextmanager
def staged_tenant_bundle(project: str, release_root: Path = ROOT):
    """A tenant's staged role-tooling bundle, as `switchyard upgrade` stages it from the installed release (SYRD-812).

    A resume, like every launch, checks the bundle its panes will run before anything starts (SYRD-249), and the
    sandbox these suites run in has neither an installed release nor a bundle -- so `resume_tenant` refused before
    the boundary its cases are about (red since 32654e78). Here the tree under test is the installed release (the
    sandbox's shared `current`, made only if absent) and the bundle is staged from it, as the invoking account,
    into the sandbox's tenant-control root; the production check confirms it. Staged by root on a host, it is judged
    here as staged by the reader, which is what the verifier's own default is for sandboxes."""
    from scripts.ticket_board import project_provision as provision

    current = team_launcher.switchyard_shared_install_root() / "current"
    made_current = not current.exists() and not current.is_symlink()
    if made_current:
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(release_root)
    release = Path(os.path.realpath(current))
    staging = Path(team_launcher.role_tooling_staging_dir(project))
    staging.mkdir(parents=True, exist_ok=True)

    def place(source: Path, target: Path, mode: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(mode)

    for name in provision.ROLE_STAGED_EXECUTABLES:
        if (release / "scripts" / name).is_file():
            place(release / "scripts" / name, staging / name, 0o755)
    for module in provision.entry_point_module_dependencies(source_root=release / "scripts"):
        if (release / "scripts" / f"{module}.py").is_file():
            place(release / "scripts" / f"{module}.py", staging / f"{module}.py", 0o644)
    template_files = provision.git_template_files(str(release))
    if Path(template_files[0][0]).is_file():
        for index, (source_file, relative, _mode) in enumerate(template_files):
            if index == 0 or Path(source_file).is_file():
                place(Path(source_file), staging / provision.GIT_TEMPLATE_DIR_NAME / relative, 0o755)
    for source, target in ((release / "scripts" / "ticket_board", staging / "ticket_board"),
                           (release / provision.SKILLS_DIR_NAME, staging / provision.SKILLS_DIR_NAME)):
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    for path in staging.rglob("*"):
        path.chmod(path.stat().st_mode & ~0o022)
    problems = team_launcher.staged_role_tooling_problems(project, str(release), staging_root=staging,
                                                          expect_uid=os.getuid())
    assert problems == [], problems
    original_ensure = team_launcher.ensure_staged_role_tooling
    team_launcher.ensure_staged_role_tooling = (
        lambda config, **kwargs: original_ensure(config, **{"expect_uid": os.getuid(), **kwargs}))
    try:
        yield staging
    finally:
        team_launcher.ensure_staged_role_tooling = original_ensure
        shutil.rmtree(staging, ignore_errors=True)
        if made_current:
            current.unlink()


def record_running_provider_state(config: object, *role_names: str) -> None:
    """A running session's provider-state record, as the launch that started it wrote it (SYRD-812).

    A launch restarts any running role whose recorded provider-state generation is not the account's current one,
    and a role with no record at all is stale by definition (SYRD-191). A fixture that models a pane already up has
    to say what its runtime was started against, or every launch of it is a restart -- which is what hid these
    cases' own checks since 6ad96d9c."""
    home = team_launcher._owner_home_for_auth(config.run_as_user or team_launcher.current_user_name())
    for role in config.roles:
        if role.role in role_names:
            generation = team_launcher.provider_state_generation(team_launcher._role_cli_name(role), owner_home=home)
            team_launcher.record_provider_state_generation(config, role, generation)
