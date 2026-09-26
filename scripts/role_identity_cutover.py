"""Cutting a tenant's roles over to their own accounts: proving identities, moving state, and rolling back.

Once the role-account migration has created per-role accounts, the running
roles still run as the owner. `cutover_role_identities_command` moves them
over, with a preview first and an apply on request:
- **Identity proof.** `canonical_role_identities` gives the accounts each
  role should run as. `running_role_identities` and
  `settled_role_identities` give what the processes actually are, probed
  `ROLE_RECOVERY_PROBE_ATTEMPTS` times, `ROLE_RECOVERY_PROBE_DELAY_SECONDS`
  apart. `role_process_identity_gaps` lists the differences.
- **The cutover.** `role_account_cutover` plans it, and
  `revert_incomplete_role_account_cutover` undoes one that did not finish.
- **Runtime state.** `repatriate_role_runtime_state` moves each role's
  runtime state into its own account, without overwriting and with the right
  owner (`_copy_tree_without_overwrite`, `_assign_tree_owner`,
  `_worktree_ownership`, `_chown_tree`).
- **Interrupted provider state.** `_interrupted_provider_state_roles` finds a
  provider-state move that was interrupted, and
  `_finish_interrupted_provider_state` completes it.
- **Readiness.** `verify_role_board_writes` proves each role can write to
  the board as itself.

Process identity (`process_uid`, `PROC_ROOT`), presentation reconnection,
starting sessions, the release transaction, the listener, board authority and
provider-state records all stay in their own places. This module reads them
through `scripts.team_launcher` when a function runs, so the suites' patches
there reach it.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-308). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.release_refs import DEFAULT_TENANT_RELEASE_DEPLOY_REF

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleAccountCutover, RoleConfig


def canonical_role_identities(config: ProjectConfig) -> dict[str, dict[str, str]]:
    """The accounts, homes and worktrees this tenant's roles will move onto."""
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    identities: dict[str, dict[str, str]] = {}
    for role in config.roles:
        account = role.run_as_user or launcher.role_account_name(config.project, role.role)
        if not account or account == owner:
            continue
        identities[role.role] = {
            "account": account,
            "home": str(launcher.home_dir_for_user(account) or Path("/home") / account),
            "worktree": role.workdir,
        }
    return identities


def running_role_identities(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, str]:
    """Which account is actually serving each role right now.

    A role is probed through the account its configuration names AND through
    the project account, because a tenant part-way onto per-role identities
    names accounts that do not exist: every probe through them fails and the
    project looks stopped while six sessions are running. That misreading is
    what makes a live tenant look fresh (SYRD-45).
    """
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    serving: dict[str, str] = {}
    for role in config.roles:
        candidates: list[str] = []
        configured = role.run_as_user
        if configured and configured != owner:
            candidates.append(configured)
        candidates.append(owner)
        for account in candidates:
            probe = launcher.role_process_runner_for(
                config, replace(role, run_as_user="" if account == owner else account), runner=runner
            )
            if probe(
                launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ).returncode != 0:
                continue
            if launcher.live_command_matches_role(role, runner=probe):
                serving[role.role] = account
                break
    return serving


# A session that has just been asked to start is not a session that has
# failed. The transaction reports on six of them at once, so a single probe
# taken the instant the launcher returns can miss the slowest and call it dead.
ROLE_RECOVERY_PROBE_ATTEMPTS = 6


ROLE_RECOVERY_PROBE_DELAY_SECONDS = 0.5


def settled_role_identities(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    expected: Sequence[str] = (),
    attempts: int | None = None,
    delay: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Which account serves each role, once the ones expected back have settled.

    Returns as soon as every expected role answers, so a healthy cutover waits
    for nothing; only a genuinely missing session costs the wait. Reporting a
    role dead because the probe was taken a moment too early is what put "every
    role did not come back" in a journal that six live sessions disagreed with
    (SYRD-61).
    """
    # Read at call time, so a test can shorten the wait without reaching into
    # a default bound when the module was imported.
    rounds = ROLE_RECOVERY_PROBE_ATTEMPTS if attempts is None else attempts
    pause = ROLE_RECOVERY_PROBE_DELAY_SECONDS if delay is None else delay
    serving = running_role_identities(config, runner=runner)
    for _attempt in range(max(0, rounds - 1)):
        if all(role in serving for role in expected):
            break
        sleep(pause)
        serving = running_role_identities(config, runner=runner)
    return serving


def role_process_identity_gaps(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    proc_root: Path | None = None,
) -> list[str]:
    """Roles whose live process is not running as the account it declares.

    Creating accounts and chowning worktrees does not move a running worker:
    the pane keeps the uid it started with. Installing an authority table that
    names the new account while the old process still serves the role is
    exactly the write blackout this ticket exists for, so the running uid is
    checked and not inferred from the account existing (SYRD-45).
    """
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    gaps: list[str] = []
    for role in config.roles:
        account = role.run_as_user
        if not account or account == owner:
            continue
        role_runner = launcher.role_process_runner_for(config, role, runner=runner)
        if role_runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            continue
        pid = launcher.pane_pid_for_role(role, runner=role_runner)
        actual = launcher.process_uid(pid, proc_root=proc_root)
        expected = launcher.uid_for_user(account)
        if actual is None or expected is None or actual != expected:
            gaps.append(
                f"{role.role}: its session runs as uid {actual} but the board would authorize "
                f"{account} (uid {expected})"
            )
    return gaps


def role_account_cutover(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    proc_root: Path | None = None,
) -> RoleAccountCutover:
    """Read the cutover state from the host rather than from intent.

    A configuration that names per-role accounts is a claim; whether those
    accounts exist, own their worktrees, hold their credentials and are the
    accounts the running role processes are actually executing as are facts.
    Any of them disagreeing is the failure this ticket exists for (SYRD-45).
    """
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    declared = tuple(
        (role.role, role.run_as_user)
        for role in config.roles
        if role.run_as_user and role.run_as_user != owner
    )
    if not declared:
        return launcher.RoleAccountCutover("complete", (), (), (), ())
    missing = tuple(account for _role, account in declared if not launcher.local_account_exists(account))
    gaps = launcher.role_isolation_gaps(config)
    unowned = tuple(gap for gap in gaps if "is not owned by" in gap)
    credentials = tuple(gap for gap in gaps if "cannot be checked" in gap or "reseed it" in gap)
    misidentified: tuple[str, ...] = ()
    if not missing and runner is not None:
        misidentified = tuple(
            role_process_identity_gaps(config, runner=runner, proc_root=proc_root)
        )
    if missing or unowned or credentials or misidentified:
        return launcher.RoleAccountCutover("partial", declared, missing, unowned, credentials, misidentified)
    return launcher.RoleAccountCutover("complete", declared, (), (), (), ())


def revert_incomplete_role_account_cutover(
    config: ProjectConfig, *, config_path: Path, dry_run: bool = False
) -> tuple[bool, str]:
    """Put a half-migrated configuration back on the identities that work.

    A configuration naming per-role accounts that do not exist is not a
    migration in progress, it is a project whose roles cannot start and whose
    live panes the board would stop recognising the moment the matching
    authority table were installed. Either every role is cut over or none is;
    anything between is reverted to the state the running sessions are in
    (SYRD-45).
    """
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"switchyard: cannot read {config_path} to repair the role-account cutover: {exc}"
    roles = payload.get("roles")
    if not isinstance(roles, list):
        return False, ""
    reverted = [
        str(role.get("role") or "")
        for role in roles
        if isinstance(role, dict) and str(role.get("run_as_user") or "").strip()
    ]
    if not reverted:
        return False, ""
    if dry_run:
        return True, (
            f"switchyard: would return {', '.join(sorted(reverted))} to the project account until "
            "their own accounts exist"
        )
    for role in roles:
        if isinstance(role, dict):
            role.pop("run_as_user", None)
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True, (
        f"switchyard: returned {', '.join(sorted(reverted))} to the project account: their own "
        "accounts do not exist yet, and a configuration naming accounts that do not exist stops "
        "those roles from starting and would cut the running panes off from the board"
    )


def _copy_tree_without_overwrite(
    source: Path, target: Path, *, owner: tuple[int, int] | None = None
) -> None:
    """Merge durable session data while never replacing owner-side state."""
    if source.is_file():
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if owner is not None:
                os.chown(target, *owner)
        return
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    if owner is not None:
        os.chown(target, *owner)
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            if owner is not None:
                os.chown(destination, *owner)
        elif path.is_file() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            if owner is not None:
                os.chown(destination, *owner)


def _assign_tree_owner(path: Path, owner: tuple[int, int] | None) -> str:
    """Give `path` and everything under it to `owner`. "" or why it was refused.

    Through the same no-following walk the ownership repair uses: this runs as
    root over a tree the tenant controls, so a symlink at the root of it, or a
    symlinked directory inside it, would otherwise carry a root-run chown
    wherever it points (SYRD-233 DAT).
    """
    from scripts import team_launcher as launcher

    if owner is None:
        return ""
    uid, gid = owner
    _findings, refusals = launcher._walk_tenant_state_tree(
        path, lambda _p, _i, chown: (chown(uid, gid), "")[1]
    )
    if refusals:
        return f"{refusals[0][0]} {refusals[0][1]}"
    return ""


def _interrupted_provider_state_roles(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    owner_home: Path,
) -> list[tuple[RoleConfig, int, str]]:
    """Live roles whose provider-state record the broken store stopped.

    Captured BEFORE the repair, because that is the only moment the evidence
    exists: afterwards the store is writable and a missing record is
    indistinguishable from a role that has never run. The record is not
    reconstructed from a guess -- the role has to be running its own configured
    CLI right now, which is what the record is about.

    Deliberately narrow. A role qualifies only while all four hold: its store is
    provably unusable, it has no record that can be read at all, its pane has a
    pid, and that pid's process tree is running the CLI the role is configured
    for. Anything else keeps SYRD-191's rule that a missing record is stale.

    Every pane question is asked through `role_process_runner_for`, because the
    command that gets here is `sudo switchyard upgrade <project>`: a bare `tmux`
    from root addresses ROOT's server, where the tenant has no sessions at all.
    Asked that way each pane answers pid 0, nothing is ever captured, the
    upgrade repairs ownership and stops, and the next launch restarts the very
    workers this exists to keep (SYRD-233 post-DAT).
    """
    from scripts import team_launcher as launcher

    captured: list[tuple[RoleConfig, int, str]] = []
    for role in config.roles:
        cli = launcher._role_cli_name(role)
        if not cli:
            continue
        if not launcher.provider_state_store_problem(config, role):
            continue
        if launcher.recorded_provider_state_generation(config, role):
            continue
        role_runner = launcher.role_process_runner_for(config, role, runner=runner)
        pane_pid = launcher.pane_pid_for_role(role, runner=role_runner)
        if pane_pid <= 0 or not launcher.live_command_matches_role(role, runner=role_runner):
            continue
        captured.append((role, pane_pid, launcher.provider_state_generation(cli, owner_home=owner_home)))
    return captured


def _finish_interrupted_provider_state(
    config: ProjectConfig,
    captured: Sequence[tuple[RoleConfig, int, str]],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    owner_home: Path,
    print_func: Callable[[str], None],
) -> bool:
    """Write the record each captured role could not write, or stop.

    The upgrade repaired the store; a record is still absent, and absent is
    stale, so the very next launch would end exactly the panes the repair was
    meant to save. Finishing the interrupted write is what makes the repair
    worth anything -- but only for a role that has not moved underneath the
    transaction. Anything unexpected stops the upgrade rather than seeding a
    generation for a runtime nobody checked.
    """
    from scripts import team_launcher as launcher

    for role, pane_pid, generation in captured:
        cli = launcher._role_cli_name(role)
        # The same crossing the capture used: root's own tmux server would show
        # every one of these panes as gone and stop the upgrade on that alone.
        role_runner = launcher.role_process_runner_for(config, role, runner=runner)
        now = launcher.pane_pid_for_role(role, runner=role_runner)
        if now != pane_pid:
            print_func(
                f"switchyard: {role.role} is no longer the process this upgrade found "
                f"(pane pid {pane_pid} is now {now or 'gone'}); its provider-state record was "
                "left unwritten and the upgrade stopped rather than vouch for a runtime it "
                "did not see"
            )
            return False
        if not launcher.live_command_matches_role(role, runner=role_runner):
            print_func(
                f"switchyard: {role.role} is no longer running {cli}; its provider-state record "
                "was left unwritten and the upgrade stopped"
            )
            return False
        if launcher.provider_state_generation(cli, owner_home=owner_home) != generation:
            print_func(
                f"switchyard: {cli}'s provider state changed while {config.project}'s role state "
                f"was being repaired; {role.role}'s record was left unwritten and the upgrade "
                "stopped, so the next launch decides with a fresh reading"
            )
            return False
        problem = launcher.provider_state_store_problem(config, role)
        if problem:
            # Not a reason to fail the upgrade. The repair changes ownership; it
            # does not conjure a store that is missing or unreachable for some
            # other reason, and nothing was claimed about this role. It stays
            # exactly as it was -- left running, and not judged -- which is what
            # the launch already does for a store it cannot use.
            print_func(
                f"switchyard: {role.role}'s provider state still cannot be recorded after the "
                f"repair: {problem}. It keeps running and is still not checked against the "
                "account's provider state"
            )
            continue
        launcher.record_provider_state_generation(config, role, generation)
        if launcher.recorded_provider_state_generation(config, role) != generation:
            print_func(
                f"switchyard: {role.role}'s provider-state record did not read back as what was "
                "just written; the upgrade stopped rather than report a role as settled"
            )
            return False
        print_func(
            f"switchyard: finished the provider-state record {role.role} could not write, so its "
            f"live {cli} (pid {pane_pid}) is not restarted by the next launch"
        )
    return True


def repatriate_role_runtime_state(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[bool, list[str]]:
    """Move resumable role state back to the project account, fail closed.

    Dedicated accounts are deliberately left intact. Their bindings disappear
    from the launcher configuration only after every live pane is stopped and
    every recorded session that existed before the copy passes the normal
    provider resume preflight from its new role-local path.
    """
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    legacy_roles = [
        role for role in config.roles
        if role.run_as_user and role.run_as_user != owner
    ]
    if not legacy_roles and config.role_state_isolation:
        return False, []
    problems: list[str] = []
    migration_roles = config.roles if not config.role_state_isolation else legacy_roles
    for role in migration_roles:
        account = role.run_as_user or owner
        role_runner = launcher._owner_process_runner(owner_user=account, runner=runner)
        probe = role_runner(
            launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if probe.returncode == 0:
            problems.append(
                f"{role.role}: {role.tmux_session} is still live as {account}; "
                "stop it at a resumable checkpoint before repatriation"
            )
    if problems:
        return False, problems
    if dry_run:
        return bool(legacy_roles or not config.role_state_isolation), []

    owner_home = launcher.home_dir_for_user(owner) or Path("/home") / owner
    try:
        owner_record = pwd.getpwnam(owner)
        owner_ids: tuple[int, int] | None = (owner_record.pw_uid, owner_record.pw_gid)
    except KeyError:
        owner_ids = None
    for role in config.roles:
        target_session_dir = config.session_dir / "roles" / role.role
        sources = [config.session_dir]
        if role.run_as_user and role.run_as_user != owner:
            sources.insert(0, launcher.account_session_dir(role.run_as_user, project=config.project))
        before: list[tuple[Path, str]] = []
        for source_dir in sources:
            record = launcher._session_record_for_role(role, source_dir)
            if record is not None:
                before.append((source_dir, str(record.get("session_id") or "")))
                for suffix in ("", ".superseded", ".resume_timeout"):
                    source = source_dir / f"{launcher.session_file_name(role.target)}{suffix}"
                    destination = target_session_dir / source.name
                    if source.is_file() and not destination.exists():
                        try:
                            copied_payload = json.loads(source.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            problems.append(f"{role.role}: cannot read session record {source}")
                            continue
                        payload_data = copied_payload.get("payload") if isinstance(copied_payload, dict) else None
                        transcript = (
                            str(payload_data.get("transcript_path") or "")
                            if isinstance(payload_data, dict) else ""
                        )
                        if transcript:
                            source_home = launcher._home_from_session_dir(source_dir)
                            try:
                                relative = Path(transcript).relative_to(source_home)
                            except ValueError:
                                pass
                            else:
                                payload_data["transcript_path"] = str(owner_home / relative)
                        launcher._write_private_json_atomic(destination, copied_payload)
                        if owner_ids is not None:
                            os.chown(destination, *owner_ids)

        if role.run_as_user and role.run_as_user != owner:
            source_home = launcher.home_dir_for_user(role.run_as_user) or Path("/home") / role.run_as_user
            cli = launcher._command_name(role.cli[0]) if role.cli else ""
            if cli == "claude":
                _copy_tree_without_overwrite(
                    launcher._claude_project_dir_for_workdir(role.workdir, home=source_home),
                    launcher._claude_project_dir_for_workdir(role.workdir, home=owner_home),
                    owner=owner_ids,
                )
            elif cli == "codex":
                _copy_tree_without_overwrite(
                    source_home / launcher.CODEX_SESSIONS_DIR_NAME,
                    owner_home / launcher.CODEX_SESSIONS_DIR_NAME,
                    owner=owner_ids,
                )
            elif cli == "agy":
                for state_name in ("conversations", "brain"):
                    _copy_tree_without_overwrite(
                        source_home / launcher.AGY_CREDENTIAL_DIR_NAME / state_name,
                        owner_home / launcher.AGY_CREDENTIAL_DIR_NAME / state_name,
                        owner=owner_ids,
                    )
            elif cli == "hermes":
                legacy_session_dir = launcher.account_session_dir(
                    role.run_as_user, project=config.project
                )
                source_hermes_home = launcher.hermes_home_for_role(
                    role, session_dir=legacy_session_dir
                )
                target_hermes_home = launcher.hermes_home_for_role(
                    role, session_dir=target_session_dir
                )
                for state_name in launcher.HERMES_PRIVATE_HOME_ENTRIES:
                    _copy_tree_without_overwrite(
                        source_hermes_home / state_name,
                        target_hermes_home / state_name,
                        owner=owner_ids,
                    )
        if before:
            migrated_id = launcher.session_id_for_role(role, target_session_dir)
            expected_id = before[0][1]
            if not migrated_id or migrated_id != expected_id:
                problems.append(f"{role.role}: recorded session {expected_id} did not copy exactly")
                continue
            ok, reason = launcher._resume_preflight_allows_attempt(
                role, migrated_id, session_dir=target_session_dir
            )
            if not ok:
                problems.append(f"{role.role}: copied session {migrated_id} is not resumable: {reason}")
        refused = _assign_tree_owner(target_session_dir, owner_ids)
        if refused:
            problems.append(f"{role.role}: {refused}")
            continue
    if problems:
        return False, problems

    previous_worktree_owners: list[tuple[Path, int, int]] = []
    for role in legacy_roles:
        worktree = Path(role.workdir)
        if not worktree.exists():
            continue
        info = worktree.stat()
        previous_worktree_owners.append((worktree, info.st_uid, info.st_gid))
        if not _chown_tree(str(worktree), f"{owner}:{owner}", runner=runner):
            problems.append(f"{role.role}: could not return worktree {worktree} to {owner}")
            break
    if problems:
        for worktree, uid, gid in previous_worktree_owners:
            _chown_tree(str(worktree), f"{uid}:{gid}", runner=runner)
        return False, problems

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    for section in (payload.get("roles", []), payload.get("retired_workflow_roles", {}).values()):
        for role in section:
            if isinstance(role, dict):
                role.pop("run_as_user", None)
    payload["role_state_isolation"] = True
    try:
        launcher._write_json_atomic(config_path, payload, owner_user=owner)
    except OSError as exc:
        for worktree, uid, gid in previous_worktree_owners:
            _chown_tree(str(worktree), f"{uid}:{gid}", runner=runner)
        return False, [f"configuration publish failed after state copy: {exc}"]
    return True, []


def _worktree_ownership(config: ProjectConfig) -> dict[str, tuple[int, int]]:
    """Who owns each role's tree right now, so it can be put back exactly."""
    owners: dict[str, tuple[int, int]] = {}
    for role in config.roles:
        path = Path(role.workdir)
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        owners[str(path)] = (info.st_uid, info.st_gid)
    return owners


def _chown_tree(
    path: str, spec: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]
) -> bool:
    return runner(["chown", "-R", spec, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0


def verify_role_board_writes(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    tooling_dir: Path | None = None,
) -> list[str]:
    """Prove each relaunched role can actually write, as itself, to this board.

    The uid being right and the board authorizing it are two different facts,
    and only the second is the one that matters to a role trying to work
    (SYRD-45).
    """
    from scripts import team_launcher as launcher

    failures: list[str] = []
    socket_path = f"/run/{config.project}-ticket-board/ticket-board.sock"
    if config_path is not None:
        recorded = str(launcher._plan_data_from_config(config, config_path).get("socket_path") or "").strip()
        if recorded:
            socket_path = recorded
    staged_tooling_dir = tooling_dir or Path(f"/usr/local/lib/switchyard/{config.project}")
    for role in config.roles:
        account = role.run_as_user
        if not account or account == (config.run_as_user or launcher.current_user_name()):
            continue
        # Named absolutely: this is not a pane, so it has no pane PATH, and the
        # client is the staged copy a role account can actually reach (SYRD-45).
        client = str(staged_tooling_dir / "ticket-board-write")
        result = runner(
            [
                "sudo", "-u", account, "-H",
                client, "--socket", socket_path, "--caller-role", role.role,
                "verify-caller",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            failures.append(
                f"{role.role} cannot write to the board as {account} "
                f"({(str(result.stderr).strip() or result.returncode)})"
            )
    return failures


def cutover_role_identities_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    launcher: Callable[..., int] | None = None,
    stopper: Callable[..., int] | None = None,
    proc_root: Path | None = None,
    tooling_dir: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    print_func: Callable[[str], None] = print,
) -> int:
    """Move a running tenant onto per-role identities as one transaction.

    Everything that changes what the live workers can do happens between the
    stop and the verified restart: the worktrees change hands, the
    configuration names the accounts, the roles come back under them, and every
    role process's uid is read from the kernel. If any of that does not hold,
    the trees, the configuration and the workers all go back to what they were
    -- restoring the configuration alone would leave the old workers running
    without write access to their own repositories (SYRD-45).
    """
    from scripts import team_launcher

    start = launcher or (
        lambda cfg: team_launcher._start_role_sessions_without_a_window(
            cfg, config_path=config_path, runner=runner
        )
    )
    # Workers only. The display slots and the window showing them are not part
    # of what this transaction changes: the roles change accounts, the slots do
    # not, and they are re-pointed in place afterwards. Calling the whole-project
    # stop here killed the viewer and all six slots, and neither the restart nor
    # the rollback opens a window, so a rolled-back tenant came back with every
    # slot connected to a viewer that no terminal displayed (SYRD-65).
    stop = stopper or (lambda cfg: team_launcher.stop_role_sessions(cfg, runner=runner, print_func=print_func))

    identities = team_launcher.read_pending_identities(config)
    if not identities or not team_launcher._role_accounts_ready(config):
        print_func(
            f"switchyard: {config.project}'s per-role accounts do not all exist yet; run the "
            "role-account artifact first."
        )
        return 1

    # Before anything is stopped. This transaction restarts every role against
    # the staged bundle, so the companion modules, the canonical skills and the
    # release the marker names all have to be this release's first. Checked here
    # rather than only in the upgrade because `switchyard cutover-roles` reaches
    # this transaction on its own (SYRD-62).
    tooling_problems = team_launcher.staged_role_tooling_problems(
        config.project,
        str((source_repo or team_launcher._repo_root()).expanduser().resolve(strict=False)),
        staging_root=tooling_dir or Path(team_launcher.role_tooling_staging_dir(config.project)),
    )
    if tooling_problems:
        for problem in tooling_problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: refusing to move {config.project} onto per-role identities: its roles "
            "would restart against a staged bundle that is not this release's. Nothing was stopped."
        )
        team_launcher.record_upgrade_phase(
            config, config_path=config_path, phase="identities", state="blocked",
            detail="; ".join(tooling_problems), dry_run=dry_run,
        )
        return 1

    serving_before = running_role_identities(config, runner=runner)
    before = sorted(serving_before)
    if dry_run:
        print_func(
            f"switchyard: would stop {', '.join(before) or 'no running roles'}, transfer each "
            f"role's worktree to its own account, move {config.project}'s configuration onto those "
            "accounts, restart every role under its own account and verify each role process uid "
            "before anything authorizes them"
        )
        return 0

    previous_config = config_path.read_bytes()
    # What the tenant could see before anything was stopped. A cutover that
    # ends -- either way -- with nobody able to see the project is not the
    # state it started in, and saying so is the only way an operator learns it
    # from the transaction rather than from the user (SYRD-65).
    presentation_was_visible = team_launcher.presentation_is_attached(
        config, config_path=config_path, runner=runner
    )
    previous_ownership = _worktree_ownership(config)
    previous_units = team_launcher.capture_installed_units(config, config_path=config_path)
    previous_listener_state = team_launcher.capture_listener_state(config, runner=runner, config_path=config_path)
    release_pointer, previous_release = team_launcher.capture_release_pointer(config, config_path=config_path)
    print_func(
        f"switchyard: stopping {', '.join(before) or 'no running roles'} to move {config.project} "
        "onto per-role accounts"
    )
    stop_result = stop(config)
    still_running = sorted(running_role_identities(config, runner=runner))
    if stop_result != 0 or still_running:
        print_func(
            f"switchyard: refusing to move {config.project}: "
            + (f"the stop exited {stop_result}. " if stop_result else "")
            + (
                f"{', '.join(still_running)} are still running. "
                if still_running
                else ""
            )
            + "Nothing was changed; a worktree that changes hands under a live worker takes its "
            "write access away mid-task."
        )
        team_launcher.record_upgrade_phase(
            config, config_path=config_path, phase="identities", state="blocked",
            detail=f"stop exited {stop_result}; still running: {', '.join(still_running) or 'none'}",
        )
        return 1

    # Only now, with quiescence proven, do the trees change hands.
    ownership_failures: list[str] = []
    for role in config.roles:
        identity = identities.get(role.role)
        if not identity or not Path(role.workdir).exists():
            continue
        if not _chown_tree(role.workdir, f"{identity['account']}:", runner=runner):
            ownership_failures.append(f"{role.role}: could not transfer {role.workdir}")

    changed, message = team_launcher.upgrade_role_accounts_in_config(config_path)
    print_func(message)
    if changed:
        config = team_launcher.load_project_config(config.project, config_path)
    authority_problems: list[str] = []
    if not ownership_failures:
        # The projection follows the configuration, and the installed units and
        # the running services follow the projection, all before the workers
        # come back -- so nothing is ever serving an authority that disagrees
        # with the identities about to arrive.
        team_launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=config_path, runner=runner, print_func=print_func
        )
        # The listener is the owner's own user unit and it reads the same
        # schema the release changes, so it comes down before the release and
        # only comes back once everything else has verified (SYRD-45).
        authority_problems = team_launcher.stop_owner_listener(
            config, runner=runner, config_path=config_path
        )
        # The unit file and the reload before the deploy, the restart after it.
        # `deploy-restart` compares the release's own production unit with what
        # is installed and refuses when they differ, because daemon-reload is
        # deliberately outside the board's deploy grant -- and this transaction
        # exists to change that unit, so with the old one still installed the
        # migration is indistinguishable from operator drift and the deploy
        # correctly refuses. Installing the file and reloading restarts nothing,
        # so the invariant that put the binary first still holds: the old board
        # is never restarted under a unit its release cannot serve, because the
        # restart comes with the new binary (SYRD-63).
        board_restarted = False
        if not authority_problems:
            authority_problems = team_launcher.install_board_authority_files(
                config, runner=runner, config_path=config_path, print_func=print_func
            )
        if not authority_problems:
            authority_problems, board_restarted = team_launcher.deploy_release_in_transaction(
                config,
                config_path=config_path,
                source_repo=source_repo or team_launcher._repo_root(),
                commit_git_dir=commit_git_dir,
                deploy_ref=deploy_ref,
                runner=runner,
                print_func=print_func,
            )
        if not authority_problems:
            authority_problems = team_launcher.activate_board_authority(
                config, runner=runner, restart=not board_restarted, print_func=print_func
            )
    # Nothing is asked to come back when an earlier step already failed: the
    # release did not resolve, or a tree did not change hands, and the workers
    # are deliberately still stopped. Probing them here and reporting each one
    # "did not come back" describes a restart that was never attempted, and it
    # is the sentence an operator reads after the rollback has already brought
    # all six back (SYRD-61).
    start_attempted = not (ownership_failures or authority_problems)
    start_result = start(config) if start_attempted else 0
    identity_gaps = (
        role_process_identity_gaps(config, runner=runner, proc_root=proc_root)
        if start_attempted
        else []
    )
    restarted = (
        sorted(settled_role_identities(config, runner=runner, expected=before))
        if start_attempted
        else []
    )
    missing_workers = [role for role in before if role not in restarted] if start_attempted else []
    write_failures = (
        verify_role_board_writes(
            config, runner=runner, config_path=config_path, tooling_dir=tooling_dir
        )
        if not (ownership_failures or authority_problems or start_result or identity_gaps or missing_workers)
        else []
    )
    # The display slots are part of what has to be true at the end: a tenant
    # whose panes show nothing is not a successful cutover.
    presentation_problems = (
        team_launcher.reconnect_presentation(config, config_path=config_path, runner=runner)
        if not (ownership_failures or authority_problems or start_result or identity_gaps or missing_workers or write_failures)
        else []
    )
    if (
        presentation_was_visible
        and not (
            ownership_failures or authority_problems or start_result or identity_gaps
            or missing_workers or write_failures or presentation_problems
        )
        and not team_launcher.presentation_is_attached(config, config_path=config_path, runner=runner)
    ):
        presentation_problems = [
            "the presentation window that was visible before the cutover is gone; the slots are "
            "connected but no terminal is displaying them"
        ]
    listener_problems = (
        team_launcher.start_owner_listener(config, runner=runner, config_path=config_path)
        if not (
            ownership_failures or authority_problems or start_result or identity_gaps
            or missing_workers or write_failures or presentation_problems
        )
        and previous_listener_state == "active"
        else []
    )
    if (
        ownership_failures or authority_problems or start_result != 0 or identity_gaps
        or missing_workers or write_failures or presentation_problems or listener_problems
    ):
        reasons = (
            ownership_failures
            + authority_problems
            + ([f"launch exited {start_result}"] if start_result else [])
            + identity_gaps
            + [f"{role} did not come back" for role in missing_workers]
            + write_failures
            + [f"the presentation did not reconnect: {problem}" for problem in presentation_problems]
            + listener_problems
        )
        print_func(f"switchyard: rolling {config.project} back: " + "; ".join(reasons))
        stop(config)
        restored: list[str] = []
        for path, (uid, gid) in previous_ownership.items():
            if not _chown_tree(path, f"{uid}:{gid}", runner=runner):
                restored.append(f"could not restore ownership of {path}")
        config_path.write_bytes(previous_config)
        config = team_launcher.load_project_config(config.project, config_path)
        # The generated authority projection follows the configuration back, so
        # nothing is left describing identities the tenant is not using.
        team_launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=config_path, runner=runner, print_func=print_func
        )
        restored.extend(team_launcher.restore_release_pointer(release_pointer, previous_release, runner=runner))
        restored.extend(
            team_launcher.restore_installed_units(
                config,
                previous_units,
                runner=runner,
                config_path=config_path,
                listener_state=previous_listener_state,
            )
        )
        rollback_result = start(config)
        # A rollback that leaves the slots blank is not a rollback. Its failures
        # belong in the same record as the ones that caused it (SYRD-45).
        presentation_rollback = team_launcher.reconnect_presentation(
            config, config_path=config_path, runner=runner
        )
        restored.extend(
            f"the presentation did not reconnect during the rollback: {problem}"
            for problem in presentation_rollback
        )
        # Re-pointed is not visible. A rollback the user cannot see is the
        # failure this ticket exists for, so it is recorded here beside the
        # reasons that caused the rollback rather than left for them to
        # discover on a blank desktop (SYRD-65).
        if (
            presentation_was_visible
            and not presentation_rollback
            and not team_launcher.presentation_is_attached(config, config_path=config_path, runner=runner)
        ):
            restored.append(
                f"the presentation window {config.project} had before the cutover is not back; "
                f"the slots are re-pointed but no terminal is displaying them. Run "
                f"`switchyard {config.project}` from the desktop session that had the window"
            )
        # Revalidated here rather than only where it was put back: the units are
        # restored before the workers are, and a listener that died in between
        # is a listener this tenant does not have. Asked of the owner's own
        # manager, through the owner's runtime directory (SYRD-61).
        if previous_listener_state == "active":
            if team_launcher.capture_listener_state(config, runner=runner, config_path=config_path) != "active":
                restored.extend(
                    f"the notification listener did not come back: {problem}"
                    for problem in team_launcher.start_owner_listener(
                        config, runner=runner, config_path=config_path
                    )
                )
        # What actually came back, read after the restart and through the
        # configuration the rollback restored -- which names the project account
        # again, so the probe has to be the restored one and not the one the
        # transaction was using when it failed (SYRD-61).
        recovered = settled_role_identities(config, runner=runner, expected=before)
        came_back = [role for role in before if role in recovered]
        still_down = [role for role in before if role not in recovered]
        accounts_serving = sorted({recovered[role] for role in came_back})
        if not before:
            recovery = "no role session was running before this transaction"
        else:
            recovery = f"after the rollback {len(came_back)} of {len(before)} role session(s) are live"
            if accounts_serving:
                recovery += f" as {', '.join(accounts_serving)}"
            if still_down:
                recovery += f"; {', '.join(still_down)} did not come back"
        print_func(f"switchyard: {recovery}")
        team_launcher.record_upgrade_phase(
            config, config_path=config_path, phase="identities", state="rolled back",
            detail="; ".join(reasons + restored + [recovery]),
        )
        if restored:
            print_func(
                "switchyard: the rollback did not restore everything: "
                + "; ".join(restored)
                + ". Each of those must be repaired before the roles can work."
            )
        if rollback_result != 0:
            print_func(
                f"switchyard: {config.project} could not be restarted after the rollback (exit "
                f"{rollback_result}); its configuration and worktrees are back on the project "
                f"account and `switchyard {config.project}` will start it."
            )
        return 1
    print_func(
        f"switchyard: {config.project} is running under per-role identities: "
        + ", ".join(f"{role.role}={role.run_as_user}" for role in config.roles if role.run_as_user)
    )
    team_launcher.record_upgrade_phase(
        config, config_path=config_path, phase="identities", state="done",
        detail=(
            f"verified {len(restarted)} role process uid(s), {len(previous_ownership)} worktree(s) "
            "and a board write from each role"
        ),
    )
    return 0
