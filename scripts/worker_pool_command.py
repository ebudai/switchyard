"""The launcher's side of a declared worker pool: its declaration and its verb.

A project declares a pool in its launcher config -- a name, a runtime, a size
(SYRD-37). This module parses that declaration (`parse_worker_pool`, used by
`team_launcher.load_project_config`), checks what bringing a worker up would
need (`worker_pool_preflight`), and runs `switchyard worker-pool`.
`scripts/worker_pool.py` holds the life of one worker once the pool is declared.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-286). `team_launcher`
imports this module at its top, and still exports every name that lived there.
This module never imports `team_launcher` at its top, so there is no import
cycle. Where it needs a launcher facility, it reads it from
`scripts.team_launcher` when the function runs. Tests patch those
(`current_user_name`, `_owner_home_for_auth` and the rest) on `team_launcher`,
and the patches keep reaching this code.
"""

from __future__ import annotations

import argparse
import errno
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from scripts.ticket_board.workflow_config import ONBOARDING_PROMPT_MAX_CHARS

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


#: What a pool worker's identity looks like: the pool's name, a separator, and
#: a number from 1. Written down once because the board, the panes, the
#: worktrees and the notification targets all have to agree on it.
WORKER_POOL_MEMBER_SEPARATOR = "-"
#: A pool is a project's declaration, so the only limits here are the ones the
#: rest of the system really has: a name that can be a role, a Unix account and
#: a tmux session, and a size somebody could plausibly run.
WORKER_POOL_MAX_SIZE = 64
#: The same bound the workflow document puts on a role's stored prompt, taken
#: from there rather than restated, so a pool cannot declare one the document
#: would then refuse at apply time.
WORKER_POOL_ONBOARDING_PROMPT_MAX_CHARS = ONBOARDING_PROMPT_MAX_CHARS


@dataclass(frozen=True)
class WorkerPool:
    """A set of interchangeable workers a project may run on demand.

    Declared as one object -- a name, a runtime, a size -- rather than as N
    role entries, because they differ only by number and a project that writes
    them out by hand has eight places to keep in step. Nothing here is specific
    to any project, runtime or size: those are what a tenant declares (SYRD-37).
    """

    name: str
    runtime: str
    size: int
    #: What the workers are for, in the board's vocabulary. Kept because the
    #: board decides what a role may do from its kind, not from its name.
    kind: str = "implementer"
    #: Whether a worker occupies a presentation slot whenever it runs. A pool
    #: larger than the window can show is the ordinary case, so the default is
    #: that workers are attached on demand rather than permanently.
    presentation: str = "on-demand"
    #: Each worker starts its ticket with a cleared session unless a project
    #: says otherwise; that is what makes a pool worker interchangeable rather
    #: than an implementer with a long memory (SYRD-135).
    ephemeral: bool = True
    #: What a worker is told when its session starts fresh. Declared once for
    #: the bench because every worker in it does the same job; empty means the
    #: workers inherit the remit of the role they are copied from, which is the
    #: right default and a poor answer for a pool whose job differs from it
    #: (SYRD-36).
    onboarding_prompt: str = ""

    @property
    def members(self) -> tuple[str, ...]:
        return tuple(
            f"{self.name}{WORKER_POOL_MEMBER_SEPARATOR}{index}"
            for index in range(1, self.size + 1)
        )


def parse_worker_pool(raw: Any, *, path: Path | str = "") -> WorkerPool | None:
    """Read a project's declared pool, refusing anything it could not run."""
    if raw in (None, {}):
        return None
    where = f"{path} " if path else ""
    if not isinstance(raw, dict):
        raise SystemExit(f"{where}worker_pool must be an object")
    name = str(raw.get("name") or "").strip()
    runtime = str(raw.get("runtime") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name):
        raise SystemExit(
            f"{where}worker_pool name must be lowercase letters, digits and dashes: {name!r}"
        )
    if not runtime:
        raise SystemExit(f"{where}worker_pool runtime is required; it is the CLI each worker runs")
    try:
        size = int(raw.get("size"))
    except (TypeError, ValueError):
        raise SystemExit(f"{where}worker_pool size must be a whole number") from None
    if not 1 <= size <= WORKER_POOL_MAX_SIZE:
        raise SystemExit(
            f"{where}worker_pool size must be between 1 and {WORKER_POOL_MAX_SIZE}: {size}"
        )
    presentation = str(raw.get("presentation") or "on-demand").strip()
    if presentation not in {"on-demand", "attached"}:
        raise SystemExit(
            f"{where}worker_pool presentation must be on-demand or attached: {presentation!r}"
        )
    prompt = str(raw.get("onboarding_prompt") or "").strip()
    if len(prompt) > WORKER_POOL_ONBOARDING_PROMPT_MAX_CHARS:
        raise SystemExit(
            f"{where}worker_pool onboarding_prompt must be at most "
            f"{WORKER_POOL_ONBOARDING_PROMPT_MAX_CHARS} characters"
        )
    unknown = set(raw) - {
        "name", "runtime", "size", "kind", "presentation", "ephemeral", "onboarding_prompt",
    }
    if unknown:
        raise SystemExit(f"{where}worker_pool has unknown field(s): {', '.join(sorted(unknown))}")
    return WorkerPool(
        name=name,
        runtime=runtime,
        size=size,
        kind=str(raw.get("kind") or "implementer").strip() or "implementer",
        presentation=presentation,
        ephemeral=raw.get("ephemeral") is not False,
        onboarding_prompt=prompt,
    )


@dataclass(frozen=True)
class WorkerPoolFinding:
    """One thing an operator has to know before a pool is brought up.

    `blocking` separates "this would stop the upgrade" from "this is what would
    change": a preflight that mixes them makes an operator read every line to
    find the one that matters.
    """

    blocking: bool
    subject: str
    detail: str


def worker_pool_member_role(config: ProjectConfig, member: str) -> RoleConfig | None:
    return next((role for role in config.roles if role.role == member), None)


def worker_pool_preflight(
    config: ProjectConfig,
    *,
    owner_home: Path | None = None,
    board_workflow: Mapping[str, Any] | None = None,
    config_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[WorkerPoolFinding]:
    """What bringing this project's declared pool up would change, and what stops it.

    Reads only: the configuration, the owner's home, and the board's own
    account of which roles it knows. Nothing here creates a role, an account, a
    worktree or a session -- an operator is owed the whole list before any of
    that, and a preflight that mutates is not one (SYRD-37).
    """
    from scripts import team_launcher as launcher

    pool = config.worker_pool
    findings: list[WorkerPoolFinding] = []
    if pool is None:
        return [
            WorkerPoolFinding(False, config.project, "declares no worker pool; nothing to prepare")
        ]
    home = owner_home or launcher._owner_home_for_auth(config.run_as_user or launcher.current_user_name())

    existing = {role.role for role in config.roles}
    collisions = sorted(set(pool.members) & existing)
    already = [member for member in collisions if _is_pool_member_role(config, member, pool)]
    conflicting = [member for member in collisions if member not in already]
    if conflicting:
        findings.append(
            WorkerPoolFinding(
                True,
                "role names",
                f"{', '.join(conflicting)} already exist and are not {pool.runtime} pool workers; "
                "a pool must not take over a role somebody else declared",
            )
        )
    findings.append(
        WorkerPoolFinding(
            False,
            "pool",
            f"{pool.name}: {pool.size} {pool.runtime} worker(s) "
            f"{pool.members[0]}..{pool.members[-1]}, {pool.kind}, "
            f"{'ephemeral' if pool.ephemeral else 'persistent'} sessions, "
            f"{pool.presentation} presentation",
        )
    )
    missing = [member for member in pool.members if member not in existing]
    findings.append(
        WorkerPoolFinding(
            False,
            "configuration",
            f"{len(missing)} worker role(s) would be added to {config.project}: "
            + (", ".join(missing) if missing else "none, every worker is already declared"),
        )
    )

    # The runtime has to exist for the account the panes run as, not for
    # whoever is reading this report.
    owner = config.run_as_user or launcher.current_user_name()
    if not launcher._owner_cli_is_installed(pool.runtime, owner_user=owner, owner_home=home, runner=runner):
        findings.append(
            WorkerPoolFinding(
                True,
                "runtime",
                f"{pool.runtime} is not installed for owner user {owner}; "
                f"{launcher._missing_cli_install_clause(pool.runtime)}",
            )
        )
    else:
        status = launcher._cli_auth_status(pool.runtime, owner_user=owner, owner_home=home, runner=runner)
        if status != "authenticated":
            findings.append(
                WorkerPoolFinding(
                    True,
                    "runtime",
                    f"{pool.runtime} is installed but reports {status} for {owner}; every worker "
                    "would open its provider's first run instead of a prompt",
                )
            )

    # The board is where a worker's identity has to exist for work to be routed
    # to it and for notifications to reach it.
    known = _board_known_roles(board_workflow)
    if known is None:
        findings.append(
            WorkerPoolFinding(
                True,
                "board",
                f"{config.project}'s board runs the built-in workflow, which names its roles in "
                "the schema: each worker needs registering through the supported add-role path "
                "before the board will route work to it or notify it",
            )
        )
    else:
        unregistered = [member for member in pool.members if member not in known]
        findings.append(
            WorkerPoolFinding(
                bool(unregistered),
                "board",
                f"{len(unregistered)} worker identit(ies) are not in the declared workflow: "
                + (", ".join(unregistered) if unregistered else "every worker is already a role"),
            )
        )

    # A pool is one ticket per worker, and the board keeps it that way by
    # diverting the extra ticket somewhere. A declared workflow that names no
    # holding destination makes the board refuse the routing outright -- the
    # director is told "configure a holding destination" at the moment they try
    # to give a busy worker a second ticket, which is the worst time to find out
    # (SYRD-31). A tenant still on the built-in workflow is not asked: it has no
    # document to name one in, and its own blocker above says so.
    document = board_workflow.get("document") if board_workflow and "document" in board_workflow else board_workflow
    if isinstance(document, Mapping) and document.get("roles"):
        queue = document.get("queue")
        if not isinstance(queue, Mapping) or not queue.get("stage") or not queue.get("assignee"):
            findings.append(
                WorkerPoolFinding(
                    True,
                    "queue",
                    f"{config.project}'s workflow names no holding destination, so the board "
                    "cannot divert a ticket routed to a worker that already holds one; declare "
                    "`queue` with a stage and the role that owns it",
                )
            )

    # Presentation: a pool larger than the window can show is ordinary, and
    # saying so is what stops somebody expecting eight panes.
    visible = [role for role in config.roles if not role.detached]
    if pool.presentation == "attached":
        free = launcher.MAX_VISIBLE_PANES_PER_WINDOW - len(visible)
        findings.append(
            WorkerPoolFinding(
                len(missing) > free,
                "presentation",
                f"every worker would hold a pane: {len(visible)} visible role(s) today plus "
                f"{len(missing)} worker(s), and a window has {launcher.MAX_VISIBLE_PANES_PER_WINDOW} "
                f"slot(s), {free} of them free"
                + (
                    ". Declare the pool `on-demand` and attach a worker when somebody asks to "
                    "watch it"
                    if len(missing) > free
                    else ""
                ),
            )
        )
    else:
        findings.append(
            WorkerPoolFinding(
                False,
                "presentation",
                f"workers run without a permanent pane; the {len(visible)} visible role(s) "
                "already configured are unchanged, and a worker is attached when somebody "
                "asks to watch it",
            )
        )

    # A declared worker is an identity and a route, not a place to work: `apply`
    # writes the document and nothing on disk, and `start` moves a tmux session
    # and nothing else. A worker with no worktree is refused at start, so it is
    # a blocker here, before anybody tries -- with the supported preparation to
    # run, rather than a report of zero blockers followed by a refusal (SYRD-278).
    unprepared = [
        member for member in already
        if not (worker_pool_member_role(config, member).workdir
                and Path(worker_pool_member_role(config, member).workdir).is_dir())
    ]
    if unprepared:
        from scripts.worker_pool import prepare_role_command

        config_arg = config_path if config_path is not None else f"<{config.project} launcher config>"
        findings.append(
            WorkerPoolFinding(
                True,
                "worktrees",
                f"{', '.join(unprepared)} {'is' if len(unprepared) == 1 else 'are'} declared but "
                f"{'has' if len(unprepared) == 1 else 'have'} no worktree yet, so `start` would "
                "refuse; `start` does not prepare a worker. Prepare each one first: "
                + "; ".join(prepare_role_command(config_arg, member) for member in unprepared),
            )
        )

    # A worktree its runtime has never been asked to trust starts a session
    # that looks alive and sits at "Trust this folder?", unable to take work --
    # both MEFP workers did, and read as ready and running (SYRD-279). Read
    # from the provider's own record, under the account the panes run as.
    untrusted = [
        member for member in already
        if member not in unprepared
        and not launcher._workdir_is_trusted(
            pool.runtime, owner_home=home, workdir=Path(worker_pool_member_role(config, member).workdir),
            **launcher._trust_kwargs(pool.runtime, worker_pool_member_role(config, member)),
        )
    ]
    if untrusted:
        from scripts.worker_pool import prepare_role_command

        config_arg = config_path if config_path is not None else f"<{config.project} launcher config>"
        findings.append(
            WorkerPoolFinding(
                True,
                "trust",
                f"{pool.runtime} has not trusted the worktree of {', '.join(untrusted)}, so "
                f"{'its session' if len(untrusted) == 1 else 'their sessions'} would open at its "
                "folder-trust prompt instead of taking work. Preparation asks once, as the owner: "
                + "; ".join(prepare_role_command(config_arg, member) for member in untrusted),
            )
        )

    findings.append(
        WorkerPoolFinding(
            False,
            "not done here",
            "this reports only. No role, account, worktree, board registration or session is "
            "created, and no existing role, ticket or credential is touched",
        )
    )
    return findings


def _is_pool_member_role(config: ProjectConfig, member: str, pool: WorkerPool) -> bool:
    from scripts import team_launcher as launcher

    role = worker_pool_member_role(config, member)
    return role is not None and launcher._role_cli_name(role) == pool.runtime


def _board_known_roles(board_workflow: Mapping[str, Any] | None) -> set[str] | None:
    """Which roles the board's declared workflow names, or None if it has no document."""
    if not board_workflow:
        return None
    document = board_workflow.get("document") if "document" in board_workflow else board_workflow
    if not isinstance(document, Mapping):
        return None
    roles = document.get("roles")
    if not isinstance(roles, list):
        return None
    return {str(role.get("name") or "") for role in roles if isinstance(role, Mapping)}


def format_worker_pool_preflight(
    config: ProjectConfig, findings: Sequence[WorkerPoolFinding]
) -> list[str]:
    blocking = [finding for finding in findings if finding.blocking]
    lines = [
        f"switchyard: worker pool preflight for {config.project}: "
        f"{len(blocking)} blocker(s), {len(findings) - len(blocking)} change(s) reported"
    ]
    for finding in findings:
        marker = "BLOCKER" if finding.blocking else "would"
        lines.append(f"  {marker:<8} {finding.subject}: {finding.detail}")
    if blocking:
        lines.append(
            "switchyard: nothing was changed. Clear the blocker(s) above and run this again."
        )
    return lines


def switchyard_worker_pool_command(
    project: str,
    *,
    action: str = "preflight",
    member: str = "",
    apply_changes: bool = False,
    force: bool = False,
    out: Path | None = None,
    journal: Path | None = None,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    board_reader: Callable[[ProjectConfig], Mapping[str, Any] | None] | None = None,
    board_snapshot_reader: Callable[[ProjectConfig], Mapping[str, Any] | None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
    dry_run: bool = False,
    watch: bool = False,
    interval: float = 60.0,
) -> int:
    """One pool's whole life, from what it would cost to which worker is holding what.

    Every verb that would change a board, a configuration or an account shows
    what it would do and writes nothing without `--apply`; the three that only
    move a tmux session -- start, stop, restart -- act, because a session is
    the one thing here that is not durable state (SYRD-37).
    """
    from scripts import team_launcher as launcher

    from scripts import worker_pool as pool_module

    entry = launcher._resolve_switchyard_project(project, config_dir=config_dir, registry_dir=registry_dir)
    try:
        config = launcher.load_project_config(entry.slug, entry.config_path)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - any failure, as release-status (SYRD-659)
        # Another tenant's private tree, a checkout that moved, or a document that does not load (SYRD-731).
        return worker_pool_without_configuration(
            entry.slug, action=action, member=member, config_path=entry.config_path, problem=exc,
            registry_dir=registry_dir, print_func=print_func,
        )
    pool = config.worker_pool
    read_board = board_reader or _read_board_workflow_document
    board_workflow = read_board(config)
    document = _worker_pool_document(config, board_workflow)

    if action == "preflight":
        findings = worker_pool_preflight(
            config, board_workflow=board_workflow, config_path=entry.config_path, runner=runner
        )
        for line in format_worker_pool_preflight(config, findings):
            print_func(line)
        if pool is not None and document:
            admission = pool_module.review_admission(
                pool_module.expand_pool(
                    document, pool, project=config.project, worktree_base=config.worktree_base
                )[0],
                pool,
            )
            for line in pool_module.format_review_admission(admission):
                print_func(line)
        return 1 if any(finding.blocking for finding in findings) else 0

    if pool is None:
        print_func(f"switchyard: {config.project} declares no worker pool; there is nothing to {action}.")
        return 1

    if action == "plan":
        readiness = _worker_pool_readiness(
            config, pool, document=document, board_snapshot_reader=board_snapshot_reader, runner=runner
        )
        steps = pool_module.upgrade_plan(
            config,
            pool,
            document=document,
            readiness=readiness,
            blockers=[
                (finding.subject, finding.detail)
                for finding in worker_pool_preflight(
                    config, board_workflow=board_workflow, config_path=entry.config_path,
                    runner=runner,
                )
                if finding.blocking
            ],
            config_path=entry.config_path,
            workflow_path=out,
        )
        for line in pool_module.format_plan(config.project, steps):
            print_func(line)
        return 1 if any(step.blocking for step in steps) else 0

    if action in {"list", "status"}:
        readiness = _worker_pool_readiness(
            config, pool, document=document, board_snapshot_reader=board_snapshot_reader, runner=runner
        )
        if not readiness:
            print_func(
                f"switchyard: {config.project} declares the {pool.name} pool but no worker is "
                f"registered yet; run `switchyard worker-pool {config.project} apply` to see how."
            )
            return 1
        running = sum(1 for state in readiness if state.session)
        ready = sum(1 for state in readiness if state.ready)
        # Capacity is its own fact: a ready, running worker whose slot a ticket
        # at User UAT holds takes nothing routed to it (SYRD-476).
        # A slot the board did not report is not counted as free, nor as held.
        if not all(state.reservation_known for state in readiness):
            capacity = "capacity unknown: the board did not report every worker's serial slot"
        else:
            free = sum(1 for state in readiness if state.can_take_work)
            held = sum(1 for state in readiness if state.reserved_by)
            capacity = f"{free} can take a ticket now, {held} held by a ticket"
            if any(state.claimant is not None for state in readiness):
                capacity += f", {sum(1 for state in readiness if state.can_claim)} can claim ready work"
        print_func(
            f"switchyard: {pool.name}: {len(readiness)} worker(s), {ready} ready, {running} running; {capacity}"
        )
        for state in readiness:
            print_func(f"  {state.describe()}")
        return 0

    if action == "admission":
        expanded = document
        if document:
            expanded, _changes = pool_module.expand_pool(
                document, pool, project=config.project, worktree_base=config.worktree_base
            )
        admission = pool_module.review_admission(expanded or {}, pool)
        if not admission.lanes:
            print_func(
                f"switchyard: {config.project} has no declared workflow to read review lanes from; "
                "a board running the built-in workflow cannot serialise one."
            )
            return 1
        # Said plainly, because the same lines mean different things before and
        # after the pool is declared: one is a promise, the other a report.
        declared = bool(pool_module.live_members(document or {}, pool))
        tense = "is" if declared else "would be"
        print_func(
            f"switchyard: review admission for {pool.name} {tense}: {len(admission.lanes)} lane(s), "
            f"{len(admission.ambiguous_lanes)} ambiguous"
            + ("" if declared else "; no worker is declared yet, so this is what applying the pool would leave")
        )
        for line in pool_module.format_review_admission(admission):
            print_func(line)
        return 1 if admission.ambiguous_lanes else 0

    if action in {"apply", "retire", "replace"}:
        if document is None:
            print_func(
                f"switchyard: {config.project}'s board runs the built-in workflow, so a pool cannot "
                "be declared in a document it does not have. Register each worker through "
                f"`switchyard add-role {config.project} {pool.name}-N --cli {pool.runtime} --detached`, "
                f"and see `switchyard worker-pool {config.project} plan` for the order and what it costs."
            )
            return 1
        if action == "apply":
            desired, changes = pool_module.expand_pool(
                document, pool, project=config.project, worktree_base=config.worktree_base
            )
        elif action == "retire":
            desired, changes = pool_module.retire_worker(document, pool, member)
        else:
            desired, changes = pool_module.replace_worker(
                document, pool, member, project=config.project, worktree_base=config.worktree_base
            )
        print_func(f"switchyard: worker pool {action} for {config.project}: {len(changes)} document change(s)")
        for change in changes:
            print_func(f"  would   {change.subject}: {change.detail}")
        if out is not None:
            out.write_text(json.dumps(desired, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print_func(f"switchyard: wrote the proposed document to {out}")
        if not apply_changes:
            print_func(
                "switchyard: nothing was changed. Review the document, then run this again with "
                "--apply, which writes it through the ordinary workflow path -- the same "
                "validation, the same rollback journal."
            )
            return 0
        return _apply_worker_pool_document(
            config, desired, config_path=entry.config_path, print_func=print_func
        )

    if action == "rollback":
        from scripts import workflow_manage

        if journal is None:
            print_func(
                "switchyard: rollback needs the journal the apply printed: "
                f"--journal <path>. `switchyard rollout-log {config.project}` has the run."
            )
            return 1
        return workflow_manage.main(
            ["rollback", "--config", str(entry.config_path), "--journal", str(journal),
             "--board-url", config.board_url]
        )

    if action in {"start", "stop", "restart"}:
        readiness = _worker_pool_readiness(
            config, pool, document=document, board_snapshot_reader=board_snapshot_reader, runner=runner
        )
        from scripts import worker_cohorts

        # A cohort member's start or stop is a cohort change, recorded as the operator's (SYRD-628).
        record = worker_cohorts.operator_change(config, document, member, action, print_func=print_func)
        for kind in (("stop", "start") if action == "restart" else (action,)) if record else ():
            record(kind, "intended")
        from scripts import worker_admission

        # An operator's stop keeps the worker down until an operator starts it again (SYRD-688).
        worker_admission.mark(config, entry.config_path, "--operator-stopped" if action == "stop" else "--operator-started",
                              member, runner=runner, print_func=print_func)
        if action == "stop":
            results = [pool_module.stop_worker(config, member, runner=runner)]
        else:
            started = pool_module.start_worker if action == "start" else pool_module.restart_worker
            outcome = started(
                config, pool, member,
                readiness=readiness, config_path=entry.config_path, force=force, runner=runner,
            )
            results = outcome if isinstance(outcome, list) else [outcome]
        for result in results:
            print_func(result.describe())
            if record:
                kind = "stop" if "stop" in result.action else "start"
                record(kind if result.succeeded else f"{kind}_failed", "done" if result.succeeded else "failed",
                       result.describe())
        # A worker that was asked for and did not come up is a failure, and so
        # is a restart whose stop worked and whose start did not: "not started"
        # exiting 0 read as done to every caller that checks (SYRD-278).
        return 0 if all(result.succeeded for result in results) else 1

    if action in {"cohorts", "converge"}:  # SYRD-628
        from scripts import worker_cohorts

        return worker_cohorts.run_command(
            config, pool, document=document, action=action, config_path=entry.config_path, dry_run=dry_run,
            watch=watch, interval=interval, runner=runner, print_func=print_func,
            read_readiness=lambda: _worker_pool_readiness(config, pool, document=document,
                                                          board_snapshot_reader=board_snapshot_reader, runner=runner),
            read_snapshot=lambda: (board_snapshot_reader or _read_board_snapshot)(config),
            read_status=lambda: _board_get_json(f"{str(config.board_url or '').rstrip('/')}/api/cohorts")
            if config.board_url else None)

    if action == "attach":
        from scripts import presentation_controller

        return presentation_controller.attach_role_command(
            config, role_name=member, json_output=False, runner=runner, print_func=print_func
        )

    raise SystemExit(f"switchyard: unknown worker-pool action {action!r}")


def _worker_pool_document(
    config: ProjectConfig, board_workflow: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The tenant's declared workflow document, or None when it runs the built-in one.

    Asked of the running board first, because what matters is the document the
    board is actually enforcing; the generated config's copy is the fallback for
    a board that cannot be reached.
    """
    if board_workflow:
        document = board_workflow.get("document") if "document" in board_workflow else board_workflow
        if isinstance(document, Mapping) and document.get("roles"):
            return dict(document)
    raw = getattr(config, "workflow", None)
    return dict(raw) if isinstance(raw, Mapping) and raw.get("roles") else None


def _worker_pool_readiness(
    config: ProjectConfig,
    pool: "WorkerPool",
    *,
    document: Mapping[str, Any] | None,
    board_snapshot_reader: Callable[[ProjectConfig], Mapping[str, Any] | None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
):
    from scripts import worker_pool as pool_module

    read_snapshot = board_snapshot_reader or _read_board_snapshot
    board = read_snapshot(config)
    # The board's own answer about each worker's serial slot rides with its
    # snapshot; a board that gave none leaves the slot unknown, never free.
    reservations = (board or {}).get("reservations") if isinstance(board, Mapping) else None
    return pool_module.worker_readiness(
        config, pool, document=document, board=board,
        reservations=reservations if isinstance(reservations, Mapping) else None, runner=runner,
    )


def _read_board_snapshot(config: ProjectConfig) -> Mapping[str, Any] | None:
    """What the board says its tickets are, and which ticket holds each worker's serial slot.

    The reservations come from the board's own gate (GET /api/reservations,
    SYRD-476) under "reservations"; a board without that route -- one older
    than it -- leaves the key out, and every slot then reads as not reported.
    """
    url = str(config.board_url or "").strip()
    if not url:
        return None
    payload = _board_get_json(f"{url.rstrip('/')}/api/board")
    if not isinstance(payload, dict):
        return None
    reported = _board_get_json(f"{url.rstrip('/')}/api/reservations")
    if isinstance(reported, dict) and isinstance(reported.get("reservations"), dict):
        payload = {**payload, "reservations": reported["reservations"]}
    return payload


def _board_get_json(url: str) -> Any:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _apply_worker_pool_document(
    config: ProjectConfig,
    document: Mapping[str, Any],
    *,
    config_path: Path,
    print_func: Callable[[str], None] = print,
) -> int:
    """Write a pool's document through the ordinary workflow path, and nowhere else.

    Deliberately not its own writer. `workflow_manage apply` validates against
    the running board, refuses on a revision race, writes the rollback journal
    BEFORE the board write, hands the journal to the tenant, and applies the
    launcher projection atomically. A second implementation of that would be a
    second set of ways to get it wrong.
    """
    import tempfile

    from scripts import workflow_manage

    with tempfile.TemporaryDirectory(prefix="switchyard-worker-pool.") as scratch:
        path = Path(scratch) / "workflow.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print_func(f"switchyard: applying through ticket-board-workflow apply --config {config_path}")
        return workflow_manage.main(
            ["apply", "--document", str(path), "--config", str(config_path),
             "--board-url", config.board_url]
        )


def _read_board_workflow_document(config: ProjectConfig) -> Mapping[str, Any] | None:
    """The board's own account of its workflow, or None when it has no document.

    Asked of the running board rather than of a file, because what matters is
    which roles it will actually route work to.
    """
    import urllib.request

    url = str(config.board_url or "").strip()
    if not url:
        return None
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/workflow", timeout=10) as response:
            if response.status != 200:
                return None
            document = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return document if isinstance(document, dict) else None


#: The verbs that can answer from the board alone, for a tenant whose configuration this account cannot use.
WORKER_POOL_BOARD_ACTIONS = frozenset({"list", "status"})


def worker_pool_without_configuration(
    slug: str,
    *,
    action: str,
    member: str = "",
    config_path: Path,
    problem: BaseException,
    registry_dir: Path | None = None,
    address: Callable[[str], tuple[int, str]] | None = None,
    board_get: Callable[[str], Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """`worker-pool` for a tenant whose generated configuration this account cannot use (SYRD-731).

    The configuration is in its owner's private tree, so another account -- the desktop operator checking a
    tenant's pool -- cannot load it, and is not given a way round that. `list` and `status` answer from the
    tenant's running board instead, at the loopback port root's own unit names (SYRD-659 reads the same port):
    which workers its workflow document declares, which tickets each holds, its serial slot, and whether a
    process is registered for it, judged as the listener judges one. What only the owner can see -- skill,
    sign-in, worktree, trust and the session itself -- is named as not read. Every other verb acts on or reads
    what only the configuration says, and refuses, naming who can run it.
    """
    from scripts import project_status
    from scripts import team_launcher as launcher

    code, said = project_status.unusable_configuration(slug, config_path=config_path, problem=problem,
                                                        registry_dir=registry_dir)
    print_func(said)
    private = code in (errno.EACCES, errno.EPERM)
    owner = launcher._project_config_path_owner_user(config_path) or "its owner"
    if action not in WORKER_POOL_BOARD_ACTIONS:
        print_func(f"switchyard: worker-pool {action} needs {slug}'s own configuration, so nothing was read or changed"
                   + (f"; run `switchyard worker-pool {slug} {' '.join(filter(None, (action, member)))}` as {owner}."
                      if private else "."))
        return 1

    from scripts import worker_pool as pool_module
    from scripts.ticket_board import board_identity
    from scripts.ticket_board.notify_listener import assignment_process_gone
    from scripts.ticket_board.read_client import queue_payload
    from scripts.ticket_board.workflow_config import pull_claimant_pool

    try:
        port, _prefix = (address or board_identity.unit_board_address)(slug)
    except (board_identity.BoardIdentityError, OSError) as exc:
        print_func(f"switchyard: {slug}'s board cannot be found from root's records, so its pool is not reported: "
                   f"{exc}")
        return 1
    base = f"http://127.0.0.1:{port}"
    read = board_get or _board_get_json
    workflow, board, reported, registered = (read(f"{base}{path}") for path in (
        "/api/workflow", "/api/board", "/api/reservations", "/api/runtime-assignments"))
    claimed = {str(answer.get("project")) for answer in (reported, registered)
               if isinstance(answer, dict) and answer.get("project") is not None}
    if claimed - {slug}:
        print_func(f"switchyard: the board at {base}, where root's unit puts {slug}'s, says it is "
                   f"{', '.join(sorted(claimed - {slug}))}'s; its pool is not reported.")
        return 1
    document = workflow.get("document") if isinstance(workflow, dict) else None
    roles = document.get("roles") if isinstance(document, dict) else None
    if not isinstance(roles, list):
        print_func(f"switchyard: {slug}'s board at {base} did not answer /api/workflow with a workflow document"
                   + ("; it runs the built-in workflow, which declares no pool." if isinstance(workflow, dict)
                      else "; is it running? `switchyard status " + slug + "`"))
        return 1
    pools: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for role in roles:
        matched = pool_module.MEMBER_RE.match(str(role.get("name") or "")) if isinstance(role, dict) else None
        if matched:
            pools.setdefault(matched.group("pool"), []).append((int(matched.group("index")), role))
    if not pools:
        print_func(f"switchyard: {slug}'s board declares no pool worker: no role in its workflow document is "
                   "named <pool>-<N>.")
        return 1

    tickets = board.get("tickets") if isinstance(board, dict) else None
    if not (isinstance(tickets, list) and all(isinstance(ticket, dict) for ticket in tickets)):
        board = None
    queues = queue_payload(dict(board), None) if board is not None else None
    reservations = reported.get("reservations") if isinstance(reported, dict) else None
    # A slot the board answered with something other than a ticket or nothing is not known, never taken as one.
    reservations = ({worker: held for worker, held in reservations.items() if held is None or isinstance(held, dict)}
                    if isinstance(reservations, dict) else None)
    assignments = registered.get("assignments") if isinstance(registered, dict) else None
    assignments = assignments if isinstance(assignments, dict) else None
    try:
        claimants = set(pull_claimant_pool(document)) if isinstance(document.get("scheduling"), dict) else None
    except (AttributeError, KeyError, TypeError):
        claimants = None

    print_func(f"switchyard: {slug} worker pool, from its board at {base} (the port root's unit names) only")
    for name, members in sorted(pools.items()):
        live = [role for _index, role in sorted(members, key=lambda item: item[0]) if role.get("active") is True]
        retired = len(members) - len(live)
        lines, running, stopped, idle = [], 0, 0, {"running": 0, "stopped": 0}
        for role in live:
            worker = str(role["name"])
            registration = assignments.get(worker) if assignments is not None else None
            if assignments is None:
                session, state = "process unknown: the board did not report its runtime assignments", ""
            elif not isinstance(registration, dict):
                session, state = "stopped: no process is registered with the board", "stopped"
            else:
                gone = assignment_process_gone(registration)
                session, state = ((f"stopped: its registered process is gone ({gone})", "stopped") if gone else
                                  (f"running: process {registration.get('process_pid')} is registered with the board",
                                   "running"))
            running, stopped = running + (state == "running"), stopped + (state == "stopped")
            holding = (tuple(str(ticket.get("id") or "") for ticket in queues.get(worker, []))
                       if queues is not None else None)
            slot = pool_module.WorkerReadiness(role=worker, target="", **pool_module._reservation_of(
                worker, reservations, board)).slot()
            if state and role.get("ephemeral") is True and holding == () and slot == "serial slot free":
                idle[state] += 1
            held = ("holding nothing" if holding == () else "holding " + ", ".join(holding) if holding
                    else "holding unknown: the board did not report its tickets")
            pull = ("" if claimants is None
                    else "; claims ready work" if worker in claimants else "; not a pull claimant")
            lines.append(f"  {worker} ({role.get('target') or 'no target'}): {session}; {held}; {slot}{pull}")
        print_func(f"switchyard: {name}: {len(live)} worker(s) declared on the board"
                   + (f", {retired} retired" if retired else "")
                   + (f"; {running} running, {stopped} stopped; idle ephemeral workers: {idle['stopped']} stopped, "
                      f"{idle['running']} running" if assignments is not None else "; process state unknown"))
        for line in lines:
            print_func(line)
    print_func("switchyard: readiness -- skill, sign-in, worktree, trust and each tmux session -- is not read "
               + (f"from this account; `switchyard worker-pool {slug} status` as {owner} reports it." if private
                  else "without a usable configuration."))
    return 0 if private else 1


#: Every verb `switchyard worker-pool` takes, and whether it names a worker.
WORKER_POOL_ACTIONS: dict[str, bool] = {
    "preflight": False,
    "plan": False,
    "list": False,
    "status": False,
    "admission": False,
    "apply": False,
    "rollback": False,
    "retire": True,
    "replace": True,
    "start": True,
    "stop": True,
    "restart": True,
    "attach": True,
    # SYRD-628: provider cohorts -- status with the dry-run plan, and drain-safe convergence.
    "cohorts": False,
    "converge": False,
}


def _build_switchyard_worker_pool_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard worker-pool",
        description=(
            "Report and run the life of a project's declared pool of interchangeable workers. "
            "Every verb that would change a board, a configuration or an account shows what it "
            "would do and writes nothing without --apply; start, stop and restart move a tmux "
            "session and nothing else."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument(
        "action",
        nargs="?",
        default="preflight",
        choices=sorted(WORKER_POOL_ACTIONS),
        help=(
            "preflight (default): what bringing the pool up would change and what would stop it. "
            "plan: the ordered upgrade, each step with the way back out of it. "
            "list/status: every worker, what it needs and what it is holding. "
            "admission: whether the review lanes this pool feeds have a head and an order. "
            "apply: declare the pool in the tenant's workflow document. "
            "retire/replace: take one worker out of service, keeping its identity forever. "
            "start/stop/restart: one worker's session. attach: watch one worker. "
            "rollback: undo an apply from its journal. "
            "cohorts: each provider cohort's target, ceiling, live/busy/idle workers and the convergence plan. "
            "converge: carry out one pass of that plan (--watch: keep converging), recording every change."
        ),
    )
    parser.add_argument("member", nargs="?", default="", help="the worker a verb acts on, e.g. impl-3")
    parser.add_argument(
        "--apply",
        dest="apply_changes",
        action="store_true",
        help="write the change instead of showing it",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="start a worker despite its readiness blockers; they are reported either way",
    )
    parser.add_argument("--out", type=Path, help="write the proposed workflow document here for review")
    parser.add_argument("--journal", type=Path, help="the apply journal a rollback reverses")
    parser.add_argument("--dry-run", action="store_true", help="converge: show the plan and change nothing")
    parser.add_argument("--watch", action="store_true", help="converge: keep converging, one pass per --interval")
    parser.add_argument("--interval", type=float, default=60.0, help="converge --watch: seconds between passes (min 10)")
    return parser
