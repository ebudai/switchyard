"""The upgrade journal, its phases and root's record of the pinned source.

- `UPGRADE_PHASES` and `UPGRADE_PHASE_OWNERS` are the order a tenant upgrade
  happens in and who owns each step; `RoleAccountCutover` says where a tenant
  is between shared-uid roles and per-role accounts.
- `read_upgrade_journal` reads the phase record -- root's trusted copy, which
  gates decisions, or the tenant's readable copy (`upgrade_journal_path`).
  `record_upgrade_phase` writes root's journal and republishes the tenant's
  copy from it (`publish_tenant_journal_projection`); an unprivileged caller
  records only an observation (`_record_upgrade_observation`), which decides
  nothing. `upgrade_phase_state` and `upgrade_phase_observation` read them.
- `record_upgrade_source`, `read_upgrade_source`,
  `upgrade_source_unavailable_reason` and `resolve_pinned_upgrade_source` keep
  and apply root's record of the release an operator pinned, written through
  `_write_privileged_json` to `privileged_upgrade_source_path`.
- `director_phase_required`, `record_release_phase_from_status`,
  `upgrade_phase_report` and `outstanding_release_phase_report` decide and
  report the phases.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-376), in their original
order. The launcher imports this module and re-exports every name, so the
upgrade's phases, `finish-upgrade`, the status and onboarding commands and
every suite that reaches these through the launcher reach the same objects,
the class included. Every launcher facility these use, and every name defined
here that another definition here reads when it runs, is read from
`team_launcher` when it runs, as it was, so a patch on the launcher still
intercepts. What runs when this module loads is bound here, as it was when the
launcher loaded it: `RoleAccountCutover`'s `dataclass` decorator and
`UPGRADE_PHASE_OWNERS`, built from `UPGRADE_PHASES` just above it. The
standard-library names are this module's own imports, the same objects. This
module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from scripts.team_launcher import DeclaredWorkflowPresence, ProjectConfig, TenantReleaseStatus


UPGRADE_JOURNAL_SCHEMA = "switchyard.upgrade-journal.v1"
# The order a tenant upgrade has to happen in, and who owns each step. The
# rollout that produced this ticket ran them interleaved: the configuration was
# rewritten to name per-role accounts before those accounts existed, and a
# Director-authority board write was attempted from the root upgrade process.
# Each phase is recorded, so an interrupted upgrade resumes rather than repeats
# and a partial state is visible instead of inferred (SYRD-45).
# The order is forced by what each phase leaves true. The identities phase is a
# single transaction over the workers, their trees, the configuration, the
# installed units and the services, and it ends with every role proved able to
# write as its own account -- against the board that is running at the time,
# which still authorizes by claim. Only then is it safe to deploy the release
# whose board enforces the per-role table, because by then the table and the
# processes already agree. The director's write comes last, under its own
# account, on a board that recognises it (SYRD-45).
UPGRADE_PHASES: tuple[tuple[str, str, str], ...] = (
    ("artifacts", "root", "regenerate generated artifacts that are safe while the current roles run"),
    ("accounts", "operator", "repatriate legacy resumable state without deleting accounts"),
    ("identities", "root", "verify worktrees and role-local state belong to the project account"),
    ("release", "operator", "deploy the board release that enforces process-bound authority"),
    ("director", "director", "migrate the declarative director onboarding through the director's own board authority"),
)
UPGRADE_PHASE_OWNERS = {name: owner for name, owner, _detail in UPGRADE_PHASES}


@dataclass(frozen=True)
class RoleAccountCutover:
    """Where a tenant actually is between shared-uid roles and per-role accounts."""

    state: str
    declared: tuple[tuple[str, str], ...]
    missing_accounts: tuple[str, ...]
    unowned_worktrees: tuple[str, ...]
    credential_gaps: tuple[str, ...]
    # SYRD-45: roles whose process is running as some other account than the one
    # the board would be told to authorize for them.
    misidentified_roles: tuple[str, ...] = ()

    @property
    def is_partial(self) -> bool:
        return self.state == "partial"

    @property
    def is_complete(self) -> bool:
        return self.state == "complete"

    @property
    def problems(self) -> list[str]:
        return [
            *[f"account {account} does not exist" for account in self.missing_accounts],
            *self.unowned_worktrees,
            *self.credential_gaps,
            *self.misidentified_roles,
        ]


def upgrade_journal_path(config: ProjectConfig, *, config_path: Path) -> Path:
    """The tenant's readable copy of the phase record. Informational only."""
    return config_path.with_name(f"{config.project}-upgrade.json")


#: Where an unprivileged command's account of a phase goes in the tenant copy.
#: Separate from `phases` because it is a different kind of claim: `phases` is
#: what root proved, and this is what somebody who cannot write root's journal
#: saw. Keeping both in one map is how a Director command came to be the only
#: record saying a deployment had happened (SYRD-117).
UPGRADE_JOURNAL_OBSERVATIONS = "observations"


def _read_journal_file(path: Path, project: str) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if str(payload.get("schema") or "") != launcher.UPGRADE_JOURNAL_SCHEMA:
        payload = {"schema": launcher.UPGRADE_JOURNAL_SCHEMA, "project": project, "phases": {}}
    payload.setdefault("phases", {})
    payload.setdefault(launcher.UPGRADE_JOURNAL_OBSERVATIONS, {})
    return payload


def read_upgrade_journal(
    config: ProjectConfig, *, config_path: Path, trusted: bool = False
) -> dict[str, Any]:
    """Read the phase record. `trusted` reads root's copy, which gates decisions."""
    from scripts import team_launcher as launcher

    if trusted:
        return launcher._read_journal_file(launcher.privileged_upgrade_journal_path(config), config.project)
    return launcher._read_journal_file(launcher.upgrade_journal_path(config, config_path=config_path), config.project)


UPGRADE_SOURCE_SCHEMA = "switchyard.upgrade-source.v1"


def privileged_upgrade_source_path(config: ProjectConfig) -> Path:
    """Root's record of the source selection this upgrade was pinned to.

    It lives beside root's phase journal, in the directory only root writes,
    because it decides what code the identities transaction deploys. The tenant
    gets no copy: a tenant that could name the release would be choosing the
    board that authorizes it (SYRD-61).
    """
    from scripts import team_launcher as launcher

    return launcher.privileged_provision_dir(
        config.project, root=launcher.switchyard_privileged_provision_root()
    ) / "upgrade-source.json"


def _write_privileged_json(path: Path, payload: Mapping[str, Any]) -> str:
    """Write root's own copy of a record, atomically. Returns a problem or ""."""
    from scripts import team_launcher as launcher

    try:
        launcher.ensure_privileged_provision_dir(path.parent)
        staged = path.with_name(f".{path.name}.new")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode("utf-8"))
            os.fchmod(descriptor, launcher.privileged_artifact_mode(path.name))
            try:
                os.fchown(descriptor, 0, 0)
            except OSError:
                # Belt-and-braces where it cannot be set; the directory this
                # lives in is root's already.
                pass
        finally:
            os.close(descriptor)
        staged.replace(path)
    except OSError as exc:
        return str(exc)
    return ""


def record_upgrade_source(
    config: ProjectConfig,
    *,
    source_repo: Path | None,
    commit_git_dir: str | None,
    deploy_ref: str,
    dry_run: bool = False,
) -> list[str]:
    """Persist the release an operator pinned, and prove it reads back exactly.

    Returns the reasons it could not be made durable, which are refusals rather
    than warnings: an upgrade that accepts a pin it cannot keep goes on to
    advertise a continuation the next phase has to guess the release for, which
    is this incident (SYRD-61).
    """
    from scripts import team_launcher as launcher

    if dry_run or os.geteuid() != 0:
        # Root's record, and root's phases read it. An unprivileged upgrade
        # regenerates the tenant's own artifacts and reports; it runs none of
        # the phases that would resolve a release, and the continuation it
        # writes says outright that no release was recorded rather than
        # implying one. So this is not a failure to record -- there was nothing
        # this process could record (SYRD-61).
        return []
    intended = {
        "source_repo": launcher.resolved_source_selection(source_repo),
        "commit_git_dir": (commit_git_dir or "").strip(),
        "deploy_ref": deploy_ref,
    }
    path = launcher.privileged_upgrade_source_path(config)
    problem = launcher._write_privileged_json(
        path,
        {
            "schema": launcher.UPGRADE_SOURCE_SCHEMA,
            "project": config.project,
            "at": datetime.now(timezone.utc).isoformat(),
            **intended,
        },
    )
    if problem:
        return [f"could not record the pinned release at {path}: {problem}"]
    # Read back through the same validation every later phase uses, so a record
    # that lands but would be refused is caught here rather than there.
    if launcher.read_upgrade_source(config) != intended:
        return [f"the pinned release at {path} did not read back as it was written"]
    return []


def read_upgrade_source(config: ProjectConfig) -> dict[str, str]:
    """The pinned release, or nothing at all if it is not a record root wrote.

    Anything group- or world-writable is refused outright, and so is a file
    belonging to somebody other than root -- reading one back would let whoever
    wrote it choose the tree root deploys. Its own reader counts as well, so
    this is testable without a root-owned sandbox; on a host only root can
    write this directory, so that is root (SYRD-61).
    """
    from scripts import team_launcher as launcher

    path = launcher.privileged_upgrade_source_path(config)
    try:
        info = path.stat()
        if info.st_mode & 0o022 or info.st_uid not in (0, os.getuid()):
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if str(payload.get("schema") or "") != launcher.UPGRADE_SOURCE_SCHEMA:
        return {}
    if str(payload.get("project") or "") != config.project:
        return {}
    return {
        key: str(payload.get(key) or "").strip()
        for key in ("source_repo", "commit_git_dir", "deploy_ref")
    }


def upgrade_source_unavailable_reason(config: ProjectConfig) -> str:
    """Why root's pin record gave this process nothing, said so it can be reported."""
    from scripts import team_launcher as launcher

    path = launcher.privileged_upgrade_source_path(config)
    try:
        path.stat()
    except FileNotFoundError:
        return f"root recorded no pinned release at {path}"
    except OSError as exc:
        return f"root's pinned-release record {path} is not readable by {launcher.current_user_name()} ({exc.strerror})"
    return f"root's pinned-release record {path} was refused as not root's own"


def resolve_pinned_upgrade_source(
    config: ProjectConfig,
    *,
    source_repo: Path | None,
    commit_git_dir: str | None,
    deploy_ref: str | None,
) -> tuple[Path | None, str | None, str, str]:
    """Fill an unpinned invocation in from root's record of what was pinned.

    An explicit argument always wins; the record only supplies what this
    invocation did not carry. That is what crosses the operator handoff: the
    accounts phase ends by asking an operator to rerun the upgrade through
    `sudo`, which scrubs the environment and passes no arguments of its own, so
    a selection that lived in either was gone by the time the identities
    transaction needed it and the release could not be resolved (SYRD-61).
    """
    from scripts import team_launcher as launcher

    recorded = launcher.read_upgrade_source(config)
    if not recorded:
        return source_repo, commit_git_dir, deploy_ref or launcher.DEFAULT_TENANT_RELEASE_DEPLOY_REF, ""
    used: list[str] = []
    if source_repo is None and recorded["source_repo"]:
        source_repo = Path(recorded["source_repo"])
        used.append(f"source {source_repo}")
    if commit_git_dir is None and recorded["commit_git_dir"]:
        commit_git_dir = recorded["commit_git_dir"]
        used.append(f"commit cache {commit_git_dir}")
    # `None` is the ref nobody asked about. `--deploy-ref origin/main` is an
    # operator saying to go back to the branch, and it has to be able to say
    # that: a default-valued argument that reads as omission would hand them the
    # commit they are trying to leave (SYRD-61).
    if deploy_ref is None and recorded["deploy_ref"]:
        deploy_ref = recorded["deploy_ref"]
        used.append(f"deploy ref {deploy_ref}")
    return (
        source_repo,
        commit_git_dir,
        deploy_ref or launcher.DEFAULT_TENANT_RELEASE_DEPLOY_REF,
        "; ".join(used),
    )


def record_upgrade_phase(
    config: ProjectConfig,
    *,
    config_path: Path,
    phase: str,
    state: str,
    detail: str = "",
    dry_run: bool = False,
) -> None:
    """Persist one phase's outcome so a retry resumes instead of repeating.

    Root's journal is the record. Only root writes a phase, only root's copy is
    read back to decide anything, and the tenant's readable copy is republished
    from it (SYRD-45).

    An unprivileged caller records an OBSERVATION instead. It cannot write
    root's journal, so a phase it wrote would be a claim nothing could
    corroborate -- and that is exactly the reported defect: `finish-upgrade` is
    unprivileged by design, so when the Director ran it the tenant copy gained
    `release: done` that root's journal could never receive, and an operator
    saw an exit-0 upgrade over an outstanding deployment (SYRD-117).
    """
    from scripts import team_launcher as launcher

    if dry_run:
        return
    entry = {
        "state": state,
        "owner": launcher.UPGRADE_PHASE_OWNERS.get(phase, ""),
        "at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }
    if os.geteuid() != 0:
        launcher._record_upgrade_observation(
            config, config_path=config_path, phase=phase, state=state, detail=detail
        )
        return
    trusted = launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
    trusted["project"] = config.project
    trusted["phases"][phase] = entry
    path = launcher.privileged_upgrade_journal_path(config)
    try:
        launcher.ensure_privileged_provision_dir(path.parent)
        staged = path.with_name(f".{path.name}.new")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (json.dumps(trusted, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            os.fchmod(descriptor, launcher.privileged_artifact_mode(path.name))
            try:
                os.fchown(descriptor, 0, 0)
                os.chown(path.parent, 0, 0)
            except OSError:
                # The directory this lives in is root's already; ownership
                # here is belt-and-braces and not worth losing the record
                # over where it cannot be set.
                pass
        finally:
            os.close(descriptor)
        staged.replace(path)
    except OSError as exc:
        print(f"switchyard: could not record {phase} for {config.project}: {exc}", file=sys.stderr)
    launcher.publish_tenant_journal_projection(config, config_path=config_path, trusted=trusted)


def publish_tenant_journal_projection(
    config: ProjectConfig, *, config_path: Path, trusted: Mapping[str, Any]
) -> None:
    """Republish the tenant's readable copy FROM root's journal.

    Root's phases go in verbatim, and an observation about a phase root has now
    recorded is dropped -- it has been answered. What survives is what root
    proved, plus notes about phases root has not spoken on, plainly marked as
    notes (SYRD-117).
    """
    from scripts import team_launcher as launcher

    existing = launcher.read_upgrade_journal(config, config_path=config_path)
    phases = dict(trusted.get("phases") or {})
    observations = {
        phase: note
        for phase, note in dict(existing.get(launcher.UPGRADE_JOURNAL_OBSERVATIONS) or {}).items()
        if phase not in phases
    }
    body = (
        json.dumps(
            {
                "schema": launcher.UPGRADE_JOURNAL_SCHEMA,
                "project": config.project,
                "phases": phases,
                launcher.UPGRADE_JOURNAL_OBSERVATIONS: observations,
                "phases_written_by": "root",
                "note": (
                    "phases are a copy of the root-owned journal and are the only phase record "
                    "anything reads; observations are what unprivileged commands saw and decide "
                    "nothing"
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    published, problem = launcher.publish_tenant_artifact(
        config, config_path.parent, launcher.upgrade_journal_path(config, config_path=config_path).name, body
    )
    if not published:
        print(problem, file=sys.stderr)


def _record_upgrade_observation(
    config: ProjectConfig, *, config_path: Path, phase: str, state: str, detail: str
) -> None:
    """Note what an unprivileged command saw, without touching `phases`."""
    from scripts import team_launcher as launcher

    journal = launcher.read_upgrade_journal(config, config_path=config_path)
    journal["project"] = config.project
    journal.setdefault(launcher.UPGRADE_JOURNAL_OBSERVATIONS, {})[phase] = {
        "state": state,
        "observed_by": launcher.current_user_name(),
        "at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }
    path = launcher.upgrade_journal_path(config, config_path=config_path)
    try:
        staged = path.with_name(f".{path.name}.new")
        staged.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staged.replace(path)
    except OSError as exc:
        print(
            f"switchyard: could not note the {phase} observation for {config.project}: {exc}",
            file=sys.stderr,
        )


def upgrade_phase_observation(journal: Mapping[str, Any], phase: str) -> Mapping[str, Any]:
    """What an unprivileged command said about a phase. Decides nothing."""
    from scripts import team_launcher as launcher

    note = (journal.get(launcher.UPGRADE_JOURNAL_OBSERVATIONS) or {}).get(phase)
    return note if isinstance(note, Mapping) else {}


def upgrade_phase_state(journal: Mapping[str, Any], phase: str) -> str:
    entry = journal.get("phases", {}).get(phase)
    return str(entry.get("state") or "") if isinstance(entry, Mapping) else ""


def director_phase_required(
    config: ProjectConfig,
    *,
    config_path: Path,
    presence: DeclaredWorkflowPresence | None = None,
) -> bool:
    """Whether a board write only the director may make applies to this tenant.

    This used to be `bool(local_config["workflow"])` and nothing else, so a
    legacy tenant -- one whose workflow was never projected into its launcher
    config because it predates declarative workflows -- answered "no" and the
    whole Director phase was declared not required, without the board being
    asked at all. Its `except SystemExit: return False` made an unreadable
    config give the same answer for a different reason (SYRD-240).

    Now it is required whenever ANY source declares a workflow, or whenever the
    config could not be read at all. Only a tenant that every source agrees has
    no workflow is exempt -- and for that tenant there is genuinely no document
    to install and no migrated onboarding to receive.
    """
    from scripts import team_launcher as launcher

    state = presence or launcher.declared_workflow_presence(config, config_path=config_path)
    if state.non_declarative_by_design and not state.declared_somewhere:
        return False
    return (
        state.declared_somewhere
        or state.legacy_without_workflow
        or bool(state.config_unreadable)
    )


def record_release_phase_from_status(
    config: ProjectConfig,
    *,
    config_path: Path,
    status: "TenantReleaseStatus | None",
    dry_run: bool = False,
) -> bool:
    """Record the release phase from what is deployed, and say whether it is done.

    The identities transaction switches the release inside itself, so after a
    successful cutover there is nothing left for an operator to deploy. Recording
    `ready` then would name a deploy that must not happen: the release enforcing
    the per-role table is already the one running (SYRD-48).

    A tenant with no board root at all reports nothing and is left `ready`, which
    is what it was before: absence of a reading is not evidence of a deploy.
    """
    from scripts import team_launcher as launcher

    deployed = status is not None and status.unchanged
    launcher.record_upgrade_phase(
        config,
        config_path=config_path,
        phase="release",
        state="done" if deployed else "ready",
        detail=(
            f"deployed release {launcher._format_release_sha(status.current_sha)} already matches "
            f"{status.deploy_ref}; the identities transaction switched it"
            if deployed
            else ""
        ),
        dry_run=dry_run,
    )
    return deployed


def upgrade_phase_report(
    config: ProjectConfig,
    *,
    config_path: Path,
    cutover: RoleAccountCutover,
    journal: Mapping[str, Any],
    #: The policy this upgrade leaves the tenant with. Passed rather than read
    #: from the configuration, because a dry run records nothing: read from the
    #: file, a dry run given a valid choice would report the tenant as missing
    #: the very policy it was just handed (SYRD-232).
    desktop_policy: Mapping[str, Any] | None = None,
    dry_run: bool = False,
) -> list[str]:
    """One line per phase: who owns it, and whether it is done."""
    from scripts import team_launcher as launcher

    lines = [f"switchyard: {config.project} upgrade phases"]
    # Asked once, not once per phase: this reaches the running board, and the
    # loop below would otherwise open a socket for every row it prints.
    presence: DeclaredWorkflowPresence | None = None
    for phase, owner, detail in launcher.UPGRADE_PHASES:
        state = launcher.upgrade_phase_state(journal, phase) or "pending"
        if phase == "accounts" and cutover.is_complete:
            state = "done"
        if phase == "director":
            if presence is None:
                presence = launcher.declared_workflow_presence(config, config_path=config_path)
            if not launcher.director_phase_required(config, config_path=config_path, presence=presence):
                state = "not required"
            elif presence.legacy_without_workflow:
                # The regression in one line: this row used to read
                # "not required" over a board running no workflow at all.
                state = "pending"
                detail = (
                    f"{detail}; the board is running no declared workflow, so the director "
                    f"is still on provisioning-scaffold onboarding. "
                    f"Run `switchyard migrate-workflow {config.project}` to see what would "
                    "be installed"
                )
        lines.append(f"  {phase:<11} {owner:<8} {state:<12} {detail}")
    # Not a journaled phase -- nothing is deployed for it -- but a launch
    # readiness the operator has to be able to see, because a tenant can finish
    # every phase above and still be unable to start a single role (SYRD-232).
    policy = desktop_policy
    if policy and policy.get("mode") in {"headless", "wayland"}:
        detail = (f"role launches would use the {policy['mode']} policy supplied for "
                  f"{config.project}; a dry run records nothing"
                  if dry_run else
                  f"role launches use the {policy['mode']} policy recorded for {config.project}")
        lines.append(f"  {'desktop':<11} {'operator':<8} {'ready':<12} {detail}")
    else:
        lines.append(f"  {'desktop':<11} {'operator':<8} {'missing':<12} "
                     f"choose one with `switchyard upgrade {config.project} --desktop-policy "
                     "headless|FILE` before any role is started")
    return lines


def outstanding_release_phase_report(
    config: ProjectConfig,
    *,
    config_path: Path,
    journal: Mapping[str, Any],
) -> list[str]:
    """Say whether an operator release phase is still owed, and name the command.

    Exit 0 from `switchyard upgrade` means the artifacts it owns are prepared.
    It does not mean the board was deployed, and it never did: the release phase
    belongs to an operator. Leaving that to be inferred from a phase table is
    how an exit-0 upgrade came to be read as a completed deployment (SYRD-117).
    """
    from scripts import team_launcher as launcher

    state = launcher.upgrade_phase_state(journal, "release")
    if state == "done":
        return [
            f"switchyard: {config.project}'s release phase is closed; artifacts are prepared and "
            "the board is deployed."
        ]
    if state == "not required":
        return []
    return [
        f"switchyard: exit 0 here means {config.project}'s generated artifacts are prepared. Its "
        f"release phase is {state or 'pending'} and is an operator's: deploy the board with the "
        "recorded sequence above, then close the phase with "
        f"`pkexec switchyard release-status {config.project} --close`, which re-verifies the live "
        "build before recording anything.",
        f"switchyard: `switchyard release-status {config.project}` compares the shared release, "
        "the deployed board, the live build and both journals at any time, and changes nothing.",
    ]
