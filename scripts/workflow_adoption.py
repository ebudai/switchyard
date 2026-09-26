"""Adopting a board's workflow as the declared one, migrating to a new declaration, and the root-vouched handoff between them.

A tenant's workflow (its stages, roles and moves) is declared in a record,
and the board runs what that record says:
- **Adoption** (`switchyard adopt-workflow`) takes a board that predates the
  record. `propose_workflow_adoption` compares the board's live workflow
  with a proposed document; `propose_legacy_workflow_adoption` builds that
  document from a legacy tenant's onboarding and columns. After a preview,
  `write_workflow_record` records the result.
- **Migration** (`switchyard migrate-workflow`): `plan_workflow_migration`
  checks a new declaration against the recorded one and the board's revision
  and digest. `effective_workflow_document` is the document as it will run.
- **The handoff.** A migration prepared by the owner is published for root
  to install (`publish_workflow_handoff`, `workflow_handoff_path`). Root reads
  it back only if its root-owned record vouches for it
  (`read_workflow_handoff`, `HandedOffWorkflow`). `install_handed_off_workflow`
  applies it during `finish-upgrade`.

Loading and verifying tenant config, trusted owner identity, the privileged
provision records, the board's declared workflow and state, and pane
declarations all stay in `scripts/team_launcher.py`. This module reads them
from there when a function runs, so the suites' patches on the launcher reach
it. Rebinding panes to a migrated workflow is not here.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-303). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


@dataclass(frozen=True)
class WorkflowAdoption:
    """What an adoption proposes, and what the board says about it."""

    document: dict | None = None
    digest: str = ""
    source: Path | None = None
    board_digest: str = ""
    difference: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    #: For a document COMPOSED rather than read: everything in it that is not a
    #: plain read of the tenant, one line each, so the operator authorizing the
    #: record sees exactly what installing it changes (SYRD-240).
    accounting: tuple[str, ...] = ()

    @property
    def adoptable(self) -> bool:
        return self.document is not None and not self.problems and not self.difference


def _canonical_workflow(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True)


def _workflow_difference(proposed: Mapping[str, Any], live: Mapping[str, Any]) -> list[str]:
    """Where the two documents disagree, named rather than counted."""
    from difflib import unified_diff

    return [
        line
        for line in unified_diff(
            _canonical_workflow(live).splitlines(),
            _canonical_workflow(proposed).splitlines(),
            fromfile="the board's declared workflow",
            tofile="the document proposed for adoption",
            lineterm="",
        )
    ]


def propose_workflow_adoption(
    slug: str,
    plan: "ProjectBoardProvision",
    config: ProjectConfig,
    config_path: Path,
    *,
    owner_uid: int | None = None,
    board_reader: Callable[[ProjectConfig], tuple[dict | None, str]] | None = None,
) -> WorkflowAdoption:
    """What root would record for this project, and everything against it.

    The document comes from the tenant's own generated plan, which is exactly
    the file root refuses to trust on its own -- so it is read the way root
    reads anything it did not write (by fd, no symlink at any component, owned
    by the project owner or root, unwritable by anybody else), validated the
    way provisioning validates it, and then checked against the workflow the
    board is actually running. Two independent things have to say the same
    thing before an operator is asked to authorize anything.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import workflow_document_digest

    problems: list[str] = []
    tenant_plan_path = config_path.parent / "plan.json"
    permitted = sorted({launcher.expected_privileged_uid(), *((owner_uid,) if owner_uid is not None else ())})
    document_holder, problem = launcher.read_plan_no_follow(
        tenant_plan_path, require_root_owned=False, require_owner_uids=permitted
    )
    if document_holder is None:
        return WorkflowAdoption(source=tenant_plan_path, problems=(problem,))
    declared = document_holder.data.get("workflow")
    if declared is None:
        return WorkflowAdoption(
            source=tenant_plan_path,
            problems=(f"{tenant_plan_path} declares no workflow, so there is nothing to adopt",),
        )
    if not isinstance(declared, dict):
        return WorkflowAdoption(
            source=tenant_plan_path,
            problems=(f"{tenant_plan_path} carries a workflow that is not a document",),
        )
    recorded_project = str(declared.get("project") or "").strip()
    if recorded_project and recorded_project != slug:
        return WorkflowAdoption(
            source=tenant_plan_path,
            problems=(
                f"{tenant_plan_path} carries a workflow for project {recorded_project!r}, "
                f"not {slug!r}",
            ),
        )
    try:
        try:
            from scripts.ticket_board.workflow_config import validate
        except ImportError:  # pragma: no cover - direct execution
            from ticket_board.workflow_config import validate

        validated = validate(declared, project=slug)
    except (ValueError, SystemExit) as exc:
        return WorkflowAdoption(
            source=tenant_plan_path,
            problems=(f"{tenant_plan_path} carries a workflow this release will not accept: {exc}",),
        )

    digest = workflow_document_digest(validated)
    reader = board_reader or launcher.read_board_declared_workflow
    live, board_problem = reader(config)
    if live is None:
        problems.append(board_problem)
        return WorkflowAdoption(
            document=validated, digest=digest, source=tenant_plan_path, problems=tuple(problems)
        )
    board_digest = workflow_document_digest(live)
    difference = [] if board_digest == digest else _workflow_difference(validated, live)
    return WorkflowAdoption(
        document=validated,
        digest=digest,
        source=tenant_plan_path,
        board_digest=board_digest,
        difference=tuple(difference),
    )


def read_board_columns(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[list[dict] | None, str]:
    """The stages the running board presents, in order, over its own socket."""
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = connection_factory or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/board")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            return None, f"the board answered HTTP {response.status} for its stages"
        columns = json.loads(body).get("columns")
    except Exception as exc:  # noqa: BLE001 - any failure to read is "cannot say"
        return None, f"the board's stages could not be read: {exc}"
    if not isinstance(columns, list):
        return None, "the board reported no stages"
    return columns, ""


def propose_legacy_workflow_adoption(
    slug: str,
    plan: "ProjectBoardProvision",
    config: ProjectConfig,
    *,
    board_reader: Callable[[ProjectConfig], tuple[dict | None, str]] | None = None,
    columns_reader: Callable[[ProjectConfig], tuple[list[dict] | None, str]] | None = None,
) -> WorkflowAdoption:
    """Compose the declared workflow of a tenant that never had one, and check it.

    For a tenant provisioned with the `default-project` seed, whose plan
    declares no workflow at all -- so there is nothing for an ordinary adoption
    to record (SYRD-240). The document is built by `legacy_workflow` from ROOT's
    plan, never the tenant's, through the same generator that wrote the
    tenant's rows; what it adds or changes is returned as `accounting`.

    It is then checked against the running board before anything is proposed:
    the board must be running no declared workflow, and every stage it presents
    must appear in the composed document, in the same order and under the same
    label. A tenant whose board has drifted from what its plan would have
    seeded is refused rather than overwritten -- the document would describe a
    board that is not there.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board import legacy_workflow
    from scripts.ticket_board.project_provision import (
        project_workflow_stages,
        project_workflow_transitions,
        workflow_document_digest,
    )

    source = launcher.privileged_baseline_plan_path(slug)
    if str(getattr(plan, "workflow_seed", "") or "") != legacy_workflow.LEGACY_SEED:
        return WorkflowAdoption(source=source, problems=(
            f"{slug} was seeded with {plan.workflow_seed!r}, and only the "
            f"{legacy_workflow.LEGACY_SEED!r} seed has a composed declarative form",
        ))
    try:
        declared = legacy_workflow.compose_legacy_workflow(
            plan,
            canonical=legacy_workflow.load_canonical(Path(__file__).resolve().parents[1]),
            stage_seeds=project_workflow_stages(plan),
            transition_seeds=project_workflow_transitions(plan),
            # What each of this tenant's panes registers as -- the reviewed
            # configuration, not the example's defaults (SYRD-262).
            panes={role.role: launcher.role_pane_declaration(role) for role in config.roles},
        )
    except legacy_workflow.LegacyWorkflowRefused as exc:
        return WorkflowAdoption(source=source, problems=(str(exc),))
    try:
        from scripts.ticket_board.workflow_config import validate

        document = validate(declared.document, project=slug)
    except (ValueError, SystemExit) as exc:
        return WorkflowAdoption(source=source, problems=(
            f"the composed workflow for {slug} does not validate: {exc}",
        ))

    live, board_problem = (board_reader or launcher.read_board_declared_workflow)(config)
    if live is not None:
        return WorkflowAdoption(source=source, problems=(
            f"{slug}'s board is already running a declared workflow; composing one from "
            "its seed would replace it, which is not what this is for",
        ))
    if "no declared workflow" not in board_problem:
        return WorkflowAdoption(source=source, problems=(board_problem,))

    columns, columns_problem = (columns_reader or read_board_columns)(config)
    if columns is None:
        return WorkflowAdoption(source=source, problems=(columns_problem,))
    live_stages = [(str(c.get("key")), str(c.get("label"))) for c in columns]
    added = {s["name"] for s in document["stages"]} - {key for key, _ in live_stages}
    composed_stages = [
        (s["name"], s["label"]) for s in document["stages"] if s["name"] not in added
    ]
    if live_stages != composed_stages:
        return WorkflowAdoption(source=source, problems=(
            f"{slug}'s board presents stages {live_stages}, but its plan would have seeded "
            f"{composed_stages}; the board has drifted from its seed, so a document composed "
            "from that seed would describe a board that is not there",
        ))

    accounting = (
        *(f"changes: {line}" for line in declared.differences),
        *(f"adds: {line}" for line in declared.additions),
        *(f"omits: {line}" for line in declared.excluded),
    )
    return WorkflowAdoption(
        document=document,
        digest=workflow_document_digest(document),
        source=source,
        accounting=accounting,
    )


def write_workflow_record(slug: str, document: Mapping[str, Any]) -> Path:
    """Publish root's copy of a declared workflow where only root can rewrite it."""
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import workflow_document_digest

    path = launcher.workflow_record_path(slug)
    payload = (
        json.dumps(
            {
                "project": slug,
                "digest": workflow_document_digest(document),
                "document": document,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    launcher.ensure_privileged_provision_dir(path.parent)
    staged = path.with_name(f".{path.name}.new")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, payload)
        os.fchown(descriptor, launcher.expected_privileged_uid(), 0)
        os.fchmod(descriptor, launcher.privileged_artifact_mode(path.name))
    finally:
        os.close(descriptor)
    staged.replace(path)
    return path


@dataclass(frozen=True)
class WorkflowMigration:
    """What installing a declared workflow on a legacy tenant would do."""

    project: str
    document: dict | None = None
    digest: str = ""
    source: str = ""
    board_revision: int = 0
    board_has_document: bool = False
    already_installed: bool = False
    problems: tuple[str, ...] = ()
    # What `workflow_manage apply` will actually write: the reviewed document
    # after the director-onboarding backfill it runs on every write. That
    # backfill at least sets its migration marker, so the board ends up
    # holding this digest, never the reviewed one (SYRD-253).
    effective_digest: str = ""

    @property
    def installable(self) -> bool:
        return self.document is not None and not self.problems and not self.already_installed

    @property
    def written_digest(self) -> str:
        return self.effective_digest or self.digest


def effective_workflow_document(document: dict, config_path: Path) -> dict:
    """The document as `workflow_manage apply` will write it.

    The same function the child calls, not a restatement of it, so the digest
    shown before `--apply` and checked after it is the one the board ends up
    holding.
    """
    from scripts.workflow_manage import _migrate_director_onboarding

    return _migrate_director_onboarding(config_path, copy.deepcopy(document))


def plan_workflow_migration(
    config: ProjectConfig,
    *,
    config_path: Path,
    board_reader: Callable[[ProjectConfig], tuple[int, dict | None, str]] | None = None,
) -> WorkflowMigration:
    """Resolve the document root would install, and what the board runs now.

    The document comes only from sources root vouches for: its own recorded
    copy first, then the workflow on its own baseline plan. The tenant's
    `plan.json` is deliberately NOT one of them -- it is the file the account
    every role runs as can write, and the document decides which roles exist
    and what each may call. Adopting it is a separate, operator-authorized
    decision (`adopt-workflow`), and this installs only what that decision
    already produced (SYRD-165, SYRD-166).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import workflow_document_digest

    problems: list[str] = []
    reader = board_reader or launcher.read_board_workflow_state
    revision, live, board_problem = reader(config)
    if board_problem:
        return WorkflowMigration(project=config.project, problems=(board_problem,))

    document, recorded_problem = launcher.recorded_declared_workflow(config.project)
    source = f"root's recorded workflow at {launcher.workflow_record_path(config.project)}"
    if document is None:
        baseline = launcher.privileged_baseline_plan_path(config.project)
        holder, baseline_problem = launcher.read_plan_no_follow(baseline, require_root_owned=True)
        candidate = holder.data.get("workflow") if holder is not None else None
        if isinstance(candidate, dict):
            document, source = candidate, f"root's baseline plan at {baseline}"
        else:
            problems.append(recorded_problem)
            if holder is None:
                problems.append(baseline_problem)
            problems.append(
                f"root holds no declared workflow for {config.project} to install. An operator "
                f"records one with `pkexec switchyard adopt-workflow {config.project}`; on a "
                "board that is running no workflow at all that needs "
                "`--despite-board '<why>'`, because there is nothing for the tenant's copy to "
                "be corroborated against"
            )
            return WorkflowMigration(
                project=config.project,
                board_revision=revision,
                board_has_document=live is not None,
                problems=tuple(problems),
            )

    try:
        try:
            from scripts.ticket_board.workflow_config import validate
        except ImportError:  # pragma: no cover - direct execution
            from ticket_board.workflow_config import validate

        validated = validate(document, project=config.project)
    except (ValueError, SystemExit) as exc:
        return WorkflowMigration(
            project=config.project,
            board_revision=revision,
            board_has_document=live is not None,
            problems=(f"{source} carries a workflow this release will not accept: {exc}",),
        )

    digest = workflow_document_digest(validated)
    effective_digest = workflow_document_digest(
        effective_workflow_document(validated, config_path)
    )
    # Either form is this workflow: the reviewed one if something wrote it
    # verbatim, the effective one if it went through `apply` -- which is what
    # makes a rerun after a successful migration a no-op.
    already = live is not None and workflow_document_digest(live) in {digest, effective_digest}
    if live is not None and not already:
        problems.append(
            f"{config.project}'s board is already running a declared workflow with a different "
            f"digest ({workflow_document_digest(live)} rather than {digest}). This installs a "
            "workflow onto a board that has none; changing one that exists is "
            "`switchyard role-prompt` or a reviewed `workflow_manage apply`"
        )
    return WorkflowMigration(
        project=config.project,
        document=validated,
        digest=digest,
        source=source,
        board_revision=revision,
        board_has_document=live is not None,
        already_installed=already,
        problems=tuple(problems),
        effective_digest=effective_digest,
    )


def switchyard_migrate_workflow_command(
    slug: str,
    *,
    apply: bool = False,
    registry_dir: Path | None = None,
    config_path: Path | None = None,
    board_reader: Callable[[ProjectConfig], tuple[int, dict | None, str]] | None = None,
    euid_getter: Callable[[], int] = os.geteuid,
    print_func: Callable[[str], None] = print,
) -> int:
    """Hand root's declared workflow to a tenant's director, for a board running none.

    This is the bounded migration SYRD-240 asks for. A legacy tenant -- one
    provisioned before declarative workflows -- runs stages, transitions and
    roles as table rows with no `workflow_configuration` document, so
    `/api/workflow` answers null and its Director keeps receiving the
    provisioning-scaffold onboarding rather than the migrated one.

    Root verifies what it vouches for and shows both digests: the reviewed one,
    and the one the board will hold after the director-onboarding backfill
    every workflow write applies. With --apply it publishes that for the
    director and writes nothing to the board: configuring a workflow is a
    director write, authorized by the director's own process on the board
    socket (SYRD-253). `switchyard finish-upgrade` makes it, through
    `workflow_manage apply`.

    Idempotent: once the board runs this workflow, in either form, a rerun
    says so and does nothing.
    """
    from scripts import team_launcher as launcher

    slug = launcher._validate_project_slug(slug)
    if config_path is not None:
        resolved_config = config_path
    else:
        resolved_config = launcher._resolve_switchyard_project(
            slug, registry_dir=registry_dir
        ).config_path
    config = launcher.load_project_config(slug, resolved_config)
    migration = plan_workflow_migration(
        config, config_path=resolved_config, board_reader=board_reader
    )

    print_func(f"switchyard: {slug} declared workflow migration")
    print_func(
        f"  board            revision {migration.board_revision}, "
        + ("running a declared workflow" if migration.board_has_document else "running NONE")
    )
    if migration.document is not None:
        print_func(f"  document         {migration.digest}")
        if migration.written_digest != migration.digest:
            print_func(
                f"  writes           {migration.written_digest} (the same document with the "
                "director-onboarding backfill every workflow write applies)"
            )
        print_func(f"  source           {migration.source}")
        print_func(
            f"  roles            {len(migration.document.get('roles') or [])}; "
            f"stages {len(migration.document.get('stages') or [])}; "
            f"transitions {len(migration.document.get('transitions') or [])}"
        )
    for problem in migration.problems:
        print_func(f"  problem          {problem}")

    if migration.already_installed:
        print_func(
            f"switchyard: {slug}'s board is already running exactly this workflow "
            f"(digest {migration.written_digest}). Nothing to do."
        )
        return 0
    if not migration.installable:
        print_func(f"switchyard: nothing was changed.")
        return 1
    if not apply:
        print_func(
            "switchyard: installing a declared workflow is not reversible to 'no declared "
            "workflow': the board can be moved between documents afterwards, but there is no "
            "way back to running none."
        )
        print_func(
            f"switchyard: dry run; nothing was written. Install it with "
            f"`pkexec switchyard migrate-workflow {slug} --apply`."
        )
        return 0
    if euid_getter() != 0:
        print_func(
            f"switchyard: handing {slug}'s reviewed workflow to its director writes a "
            f"root-owned record. Run: pkexec switchyard migrate-workflow {slug} --apply"
        )
        return 1
    # Root makes no board write here. Configuring the workflow is a director
    # write, and the board decides who is the director from the process that
    # connects to its socket. Root has no such process, and a role header or a
    # write token supplied from root would be exactly the impersonation that
    # boundary exists to refuse (SYRD-253). So root publishes what it reviewed,
    # where the director can read it and nothing but root can write it, and the
    # director's own session installs it.
    try:
        handoff = publish_workflow_handoff(slug, migration)
    except OSError as exc:
        print_func(f"switchyard: could not record the handoff for {slug}'s director: {exc}")
        print_func("switchyard: nothing was changed on the board.")
        return 1
    print_func(f"switchyard: recorded {slug}'s reviewed workflow for its director at {handoff}")
    print_func(
        f"switchyard: nothing was written to the board. {slug}'s director installs it from "
        f"its own session: `switchyard finish-upgrade {slug}`. The board will then serve "
        f"digest {migration.written_digest}; rerun this command afterwards to confirm it."
    )
    return 0


def workflow_handoff_path(project: str) -> Path:
    """Where root leaves a reviewed workflow for the director to install.

    Beside root's provision directory rather than in it: that directory is
    root's alone (0700), and the director must be able to read this. Nothing
    but root may write here, like the project registry beside it.
    """
    from scripts import team_launcher as launcher

    return launcher.switchyard_privileged_provision_root().parent / "workflow-handoff" / f"{project}.json"


def publish_workflow_handoff(project: str, migration: "WorkflowMigration") -> Path:
    """Write the reviewed document, and what it will become, for the director."""
    path = workflow_handoff_path(project)
    directory = path.parent
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{directory} is not a plain directory")
    if info.st_uid != os.geteuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise OSError(
            f"{directory} is owned by uid {info.st_uid} with mode "
            f"{stat.S_IMODE(info.st_mode):04o}; only root may write where the director reads"
        )
    payload = json.dumps(
        {
            "project": project,
            "reviewed_digest": migration.digest,
            "effective_digest": migration.written_digest,
            "board_revision": migration.board_revision,
            "document": migration.document,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{project}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


@dataclass(frozen=True)
class HandedOffWorkflow:
    """A workflow root reviewed, read back the way the director may trust it."""

    document: dict
    reviewed_digest: str
    effective_digest: str


def read_workflow_handoff(
    project: str, *, config_path: Path
) -> tuple[HandedOffWorkflow | None, str]:
    """Root's handoff, or why it may not be used.

    Read by fd with no symlink at any component, and only if root wrote it
    and nobody else can rewrite it. The effective digest is recomputed with
    the function `workflow_manage apply` runs, so what the director writes is
    what root showed -- or nothing.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import workflow_document_digest

    path = workflow_handoff_path(project)
    holder, problem = launcher.read_plan_no_follow(
        path, require_root_owned=True, require_single_link=True
    )
    if holder is None:
        return None, problem
    raw = holder.data
    if str(raw.get("project") or "") != project:
        return None, f"{path} is for project {raw.get('project')!r}, not {project!r}"
    document = raw.get("document")
    if not isinstance(document, dict):
        return None, f"{path} carries no document"
    reviewed = workflow_document_digest(document)
    if reviewed != str(raw.get("reviewed_digest") or ""):
        return None, (
            f"{path} does not match its own digest ({raw.get('reviewed_digest')} vs "
            f"{reviewed}); it was changed by something that did not write it"
        )
    effective = workflow_document_digest(effective_workflow_document(document, config_path))
    if effective != str(raw.get("effective_digest") or ""):
        return None, (
            f"the document would now be written as {effective}, not the "
            f"{raw.get('effective_digest')} root showed; the director's onboarding source "
            f"changed since. Rerun `pkexec switchyard migrate-workflow {project} --apply`."
        )
    return HandedOffWorkflow(document, reviewed, effective), ""


def install_handed_off_workflow(
    config: ProjectConfig,
    *,
    config_path: Path,
    caller_role: str,
    board_reader: Callable[[ProjectConfig], tuple[int, dict | None, str]] | None = None,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> bool | None:
    """The director's first write of a workflow root reviewed.

    With dry_run, every check below is made and the write is only described:
    True then means "would install" (SYRD-254).

    Returns None when there is nothing to do (the board already runs a
    workflow, or root handed nothing over), True once the board serves exactly
    the handed-off document, and False on any refusal -- with the reason
    printed and nothing written.

    The write goes through `workflow_manage apply`, the path every later
    workflow change takes, and ONLY over the board's socket. There the board
    takes the caller's role from the connecting process, so this succeeds
    from the director's registered session and from nothing else; the role
    named here only satisfies the client.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import workflow_document_digest

    # The handoff first: it is a file, and a tenant root handed nothing to is
    # every tenant but one mid-migration, so finish-upgrade must not start
    # depending on the board being readable for them.
    if not workflow_handoff_path(config.project).exists():
        return None
    reader = board_reader or launcher.read_board_workflow_state
    revision, live, problem = reader(config)
    if problem:
        print_func(f"switchyard: {config.project}'s board workflow could not be read: {problem}")
        return False
    if live is not None:
        return None
    handed, problem = read_workflow_handoff(config.project, config_path=config_path)
    if handed is None:
        print_func(f"switchyard: not installing {config.project}'s workflow: {problem}")
        return False
    if not config.board_socket:
        print_func(
            f"switchyard: {config.project} names no board socket, and this write is only "
            "made over the socket."
        )
        return False
    if dry_run:
        print_func(
            f"switchyard: would install {config.project}'s handed-off workflow over "
            f"{config.board_socket} at board revision {revision} (digest "
            f"{handed.effective_digest}; reviewed by root as {handed.reviewed_digest}). "
            "This is the one-way step: the board cannot go back to running none."
        )
        return True
    import contextlib
    import io as _io

    from scripts import workflow_manage

    board_root = (
        str(config.board_url or "").rstrip("/").removesuffix("/api/tickets").removesuffix("/api")
    )
    with tempfile.TemporaryDirectory(prefix=f"switchyard-install-{config.project}.") as raw:
        document_path = Path(raw) / "document.json"
        document_path.write_text(
            json.dumps(handed.document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        captured = _io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                workflow_manage.main([
                    "apply",
                    "--config", str(config_path),
                    "--board-url", board_root,
                    "--socket", config.board_socket,
                    "--caller-role", caller_role,
                    "--document", str(document_path),
                    # First activation needs a validated baseline to return
                    # to. There is no "no workflow" to return to, so it is this
                    # same document: the one-way door the preview names.
                    "--rollback-document", str(document_path),
                    "--expected-revision", str(revision),
                ])
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - reported, never swallowed
            print_func(f"switchyard: {config.project}'s workflow was not installed: {exc}")
            return False
    revision, live, problem = reader(config)
    if live is None:
        print_func(
            f"switchyard: the write reported success but {config.project}'s board still "
            f"reports no declared workflow{(' (' + problem + ')') if problem else ''}."
        )
        return False
    if workflow_document_digest(live) != handed.effective_digest:
        print_func(
            f"switchyard: {config.project}'s board now runs digest "
            f"{workflow_document_digest(live)}, not the {handed.effective_digest} root handed over."
        )
        return False
    print_func(
        f"switchyard: {config.project} is running its declared workflow at revision {revision} "
        f"(digest {handed.effective_digest}; reviewed by root as {handed.reviewed_digest})."
    )
    return True


def switchyard_adopt_workflow_command(
    slug: str,
    *,
    apply: bool = False,
    despite_board: str = "",
    registry_dir: Path | None = None,
    config_path: Path | None = None,
    euid_getter: Callable[[], int] = os.geteuid,
    operator_resolver: Callable[[], Any] | None = None,
    board_reader: Callable[[ProjectConfig], tuple[dict | None, str]] | None = None,
    columns_reader: Callable[[ProjectConfig], tuple[list[dict] | None, str]] | None = None,
    journal: Any | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Record an existing tenant's declared workflow as root's own, on purpose.

    A project provisioned before root kept this record has its declared
    workflow in one place only: a file the account every role runs as can
    write. Root will not adopt that on its own -- the document decides which
    roles exist and what each of them may call -- so adoption is an operator's
    decision, taken once, with everything it rests on put in front of them
    first: the exact document, its digest, the digest the running board holds,
    and every line where the two differ.

    What makes it safe is not any one of those. It is that the tenant's file
    and the running board have to agree, that a human authorized the run
    through Polkit rather than a script having inherited root, and that what
    was shown and decided is in the rollout journal afterwards (SYRD-166).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.rollout_journal import Attempt, resolve_operator

    slug = launcher._validate_project_slug(slug)
    said: list[str] = []

    def say(line: str) -> None:
        said.append(line)
        print_func(line)

    if euid_getter() != 0:
        print_func(
            f"switchyard: adopting {slug}'s declared workflow writes root's own copy of it. "
            f"Run it the way privileged switchyard steps are run on this host: "
            f"pkexec switchyard adopt-workflow {slug}"
        )
        return 1
    operator = (operator_resolver or resolve_operator)()
    if getattr(operator, "source", "") != "pkexec" or not getattr(operator, "known", False):
        print_func(
            f"switchyard: adopting a declared workflow is an operator's decision and has to be "
            f"authorized as one. This run was elevated by "
            f"{getattr(operator, 'source', None) or 'nothing that names a person'}, so there is "
            f"nobody to record it against. Run: pkexec switchyard adopt-workflow {slug}"
        )
        return 1

    baseline = launcher.privileged_baseline_plan_path(slug)
    if launcher.partial_provision_record(slug) is None:
        print_func(
            f"switchyard: root holds no provisioning record for {slug} at {baseline}, so there "
            "is no project here to adopt a workflow for."
        )
        return 1
    document, problem = launcher.read_plan_no_follow(baseline, require_root_owned=True)
    if document is None:
        print_func(f"switchyard: {problem}")
        return 1
    identity = launcher.trusted_owner_identity(slug)
    if not identity.trusted:
        for objection in identity.problems:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to adopt a workflow for {slug}: root cannot establish whose "
            "installation this is. Nothing was changed."
        )
        return 1

    existing, existing_problem = launcher.recorded_declared_workflow(slug)
    if existing is not None:
        print_func(
            f"switchyard: root already holds {slug}'s declared workflow at "
            f"{launcher.workflow_record_path(slug)}. Nothing was changed."
        )
        return 0
    if "holds no recorded workflow" not in existing_problem:
        print_func(f"switchyard: {existing_problem}")
        print_func(
            f"switchyard: refusing to replace a record this cannot read. Nothing was changed."
        )
        return 1

    # The release root recorded for this project, not a freshly selected one.
    # Adoption installs nothing, so it needs a plan to check the tenant's
    # configuration against rather than an audited release to render from --
    # and the recorded one is what that configuration was generated beside.
    recorded_release = str(document.data.get("source_repo") or "").strip()
    if recorded_release:
        selected = Path(recorded_release)
    else:
        selected, release_problem = launcher._resume_source_release(None)
        if release_problem:
            print_func(f"switchyard: {release_problem}")
            return 1
    plan, divergence = launcher._resume_plan_from_record(document, identity, source_repo=selected)
    if divergence:
        for objection in divergence:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to adopt a workflow for {slug}: root cannot rebuild its plan "
            "without changing what it installs. Nothing was changed."
        )
        return 1
    verified, config, config_problems = launcher.verified_tenant_config(
        plan, slug, explicit=config_path, owner_uid=launcher.uid_for_user(plan.owner_user),
        registry_dir=registry_dir,
        board_reader=board_reader or launcher.read_board_declared_workflow,
    )
    if config is None or verified is None:
        for objection in config_problems:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to adopt a workflow for {slug} from a configuration root has "
            "not verified. Nothing was changed."
        )
        return 1

    attempt = journal or Attempt(
        slug,
        ["switchyard", "adopt-workflow", slug, *(["--apply"] if apply else [])],
        operator=operator.name,
    )
    # The person this command verified, and the mechanism that named them, are
    # what the record should carry -- rather than the journal deriving it a
    # second time from an environment that has already been checked here.
    attempt.operator = operator
    attempt.open()
    status, exit_status, detail = "failed", 1, ""
    try:
        proposal = propose_workflow_adoption(
            slug, plan, config, verified,
            owner_uid=launcher.uid_for_user(plan.owner_user),
            board_reader=board_reader,
        )
        composed = False
        # Only a tenant that is legacy on BOTH counts: its plan declares no
        # workflow AND its board is running none. A board that is running a
        # declared workflow belongs to a declarative tenant whose plan file has
        # lost its key -- a different fault, which the ordinary refusal names
        # correctly -- and composing a legacy document for it would describe a
        # workflow that tenant does not run. Narrowing here keeps that refusal
        # exactly as SYRD-166 defined it.
        board_runs_none = "no declared workflow" in (board_reader or launcher.read_board_declared_workflow)(config)[1]
        if proposal.document is None and board_runs_none and any(
            "declares no workflow" in problem for problem in proposal.problems
        ):
            # A tenant that predates declared workflows has nothing to adopt,
            # which is where live UAT on mefp stopped (SYRD-240). Its document
            # is composed from ROOT's plan instead, checked against the running
            # board, and put in front of the operator with everything it
            # changes -- then recorded exactly as an adopted one would be.
            proposal = propose_legacy_workflow_adoption(
                slug, plan, config,
                board_reader=board_reader,
                columns_reader=columns_reader,
            )
            composed = True
        say(f"switchyard: {slug} declared workflow "
            f"{'composed from' if composed else 'proposed from'} {proposal.source}")
        for line in proposal.accounting:
            say(f"switchyard:   {line}")
        if proposal.document is not None:
            say(f"switchyard: proposed digest {proposal.digest}")
            say(f"switchyard: the board holds {proposal.board_digest or 'no declared workflow'}")
            say("switchyard: the document being proposed:")
            for line in _canonical_workflow(proposal.document).splitlines():
                say(f"    {line}")
        for objection in proposal.problems:
            say(f"switchyard: {objection}")
        if proposal.difference:
            say(
                "switchyard: the proposed document and the workflow the board is running are "
                "not the same:"
            )
            for line in proposal.difference:
                say(f"    {line}")

        if proposal.document is None or (proposal.problems and not despite_board):
            say(f"switchyard: refusing to adopt a workflow for {slug}. Nothing was changed.")
            detail = "refused"
            return 1
        if (proposal.difference or proposal.problems) and not despite_board:
            say(
                f"switchyard: refusing to adopt a workflow the board is not running. If this "
                f"difference is the recovery -- a board that lost its configuration, say -- run "
                f"it again with --despite-board '<why>' and that reason is recorded here with "
                f"everything above."
            )
            detail = "refused: board disagreement"
            return 1
        if despite_board and (proposal.difference or proposal.problems):
            say(f"switchyard: adopting despite the board, on this reason: {despite_board}")
        if not apply:
            say(
                f"switchyard: dry run; nothing was written. Adopt it with "
                f"`pkexec switchyard adopt-workflow {slug} --apply`."
            )
            status, exit_status, detail = "completed", 0, "dry-run"
            return 0

        recorded_path = write_workflow_record(slug, proposal.document)
        stored, stored_problem = launcher.recorded_declared_workflow(slug)
        if stored is None:
            say(f"switchyard: {stored_problem}")
            say(f"switchyard: the record at {recorded_path} did not read back. Nothing is adopted.")
            detail = "record did not read back"
            return 1
        say(
            f"switchyard: recorded {slug}'s declared workflow at {recorded_path} "
            f"(digest {proposal.digest}, adopted by {operator.name})"
        )
        say(
            f"switchyard: `switchyard upgrade {slug}` and `switchyard resume-provision {slug}` "
            "now regenerate this project's declared workflow from root's own copy."
        )
        status, exit_status, detail = "completed", 0, "adopted"
        return 0
    finally:
        attempt.write("stdout", "\n".join(said) + "\n")
        attempt.close(status=status, exit_status=exit_status, detail=detail)


def _build_switchyard_migrate_workflow_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard migrate-workflow",
        description=(
            "Install root's declared workflow onto a tenant whose board is running none. "
            "A tenant provisioned before declarative workflows keeps its stages, "
            "transitions and roles as table rows with no workflow document, so "
            "/api/workflow answers null and its director keeps receiving the "
            "provisioning-scaffold onboarding instead of the migrated one. Reports what "
            "would be installed and changes nothing without --apply."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument(
        "--apply", action="store_true", help="install the workflow; requires root"
    )
    parser.add_argument("--config", dest="config_path", type=Path, default=None)
    return parser


def _build_switchyard_adopt_workflow_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard adopt-workflow",
        description=(
            "Record an existing project's declared workflow as root's own copy. Shows the "
            "document, its digest, the digest the running board holds and every difference "
            "between them, and writes nothing without --apply. The run has to be authorized "
            "through Polkit and is kept in the rollout journal."
        ),
    )
    parser.add_argument("project", help="the project slug root holds a provisioning record for")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the record; without it this shows what would be adopted and changes nothing",
    )
    parser.add_argument(
        "--despite-board",
        default="",
        metavar="REASON",
        help=(
            "adopt a document the running board is not enforcing, for a board that lost its "
            "configuration; the reason is recorded with everything else"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        dest="config_path",
        help="the generated launcher configuration, for a checkout that has moved",
    )
    return parser
