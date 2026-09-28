"""Where a tenant's declared workflow exists, asked of each source separately.

- `DeclaredWorkflowPresence` records, per source, whether a declared workflow
  exists for a tenant: its generated config (or why that could not be read),
  root's recorded copy, its plan, and the board it runs (or why the board
  could not say) -- and whether it declares none by design.
- `declared_workflow_presence` asks each of those sources, in that order, and
  keeps the answers apart: an unreadable config is not an exempt tenant, and
  an unreachable board is not one running no workflow.
- `NON_DECLARATIVE_WORKFLOW_SEED` is the seed of the tenant that keeps
  schema.sql's own workflow, which the legacy detection leaves alone.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-398), in their original
order. The launcher imports this module and re-exports every name, so the
release and upgrade records that read the reader through it, the modules that
name the record there for their annotations, and every suite that calls or
patches these there reach the same objects. The launcher facilities -- the
JSON loader, root's recorded workflow and the board's -- and the seed and the
record the reader uses are read from `team_launcher` when it runs, as they
were, so a patch on the launcher still intercepts. The record's decorator and
field defaults are bound when the class is defined, as they were. The
standard-library names are this module's own imports, the same objects.
`ProjectConfig` is imported for annotations only. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


#: The workflow seed of a tenant that keeps schema.sql's own workflow and
#: declares no per-project document. Such a tenant is not a legacy tenant: it
#: has nothing to adopt and no scaffold onboarding to migrate away from, so the
#: no-workflow detection has to leave it alone (SYRD-240).
NON_DECLARATIVE_WORKFLOW_SEED = "pgu-full"


@dataclass(frozen=True)
class DeclaredWorkflowPresence:
    """Where a declared workflow exists for this tenant, asked of each source.

    One boolean cannot answer this. "The tenant's local config has no workflow
    key" and "this tenant has no workflow" are different statements, and
    treating the first as the second is the regression: a legacy tenant, whose
    workflow was never projected locally because it predates declarative
    workflows entirely, was declared exempt from the Director phase without the
    board ever being asked (SYRD-240).

    So each source is recorded separately, and `unreadable` is kept apart from
    absent -- a config that cannot be parsed is not a tenant that needs
    nothing.
    """

    project: str
    #: The tenant's generated launcher config, which is what used to decide
    #: this on its own.
    config_declares: bool = False
    config_unreadable: str = ""
    #: Root's own recorded copy, the one `plan_workflow_from_root` prefers.
    root_records: bool = False
    #: The tenant's plan, which is where `propose_workflow_adoption` reads from.
    plan_declares: bool = False
    #: What the running board is actually enforcing, and why it could not say.
    board_document: bool = False
    board_problem: str = ""
    #: A tenant that keeps the workflow seeded by schema.sql and declares no
    #: per-project document. `pgu` is the one, and it is not a legacy tenant:
    #: it has nothing to adopt and nothing to migrate.
    non_declarative_by_design: bool = False

    @property
    def declared_somewhere(self) -> bool:
        """Whether any source says this tenant has a declared workflow."""
        return self.config_declares or self.root_records or self.plan_declares or self.board_document

    @property
    def board_runs_none(self) -> bool:
        """The board is reachable and carries no document.

        Not the same as "the board could not be read": an unreachable board is
        unknown, and reporting unknown as absent is how a transient failure
        would become a migration.
        """
        return not self.board_document and "no declared workflow" in self.board_problem

    @property
    def legacy_without_workflow(self) -> bool:
        """The condition this ticket exists for.

        The board a tenant is running is enforcing no declared workflow. That
        tenant's Director is on provisioning-scaffold onboarding by
        construction -- the pane hook's scaffold branch is gated on
        `TICKET_BOARD_ROLE_ONBOARDING_MIGRATED`, which exists only when a
        workflow document carries the migration marker -- and it is the state
        the upgrade used to close over.

        Deliberately NOT conditioned on some other source already declaring a
        workflow. A tenant provisioned before declarative workflows existed has
        one nowhere, which is precisely why it needs the migration; requiring a
        declaration first would exempt every tenant this ticket is about.

        The one exclusion is a tenant that declares none by design, which has
        no document to install and no scaffold to migrate away from.
        """
        return self.board_runs_none and not self.non_declarative_by_design


def declared_workflow_presence(
    config: ProjectConfig,
    *,
    config_path: Path,
    board_reader: Callable[[ProjectConfig], tuple[dict | None, str]] | None = None,
) -> DeclaredWorkflowPresence:
    """Ask every source that can hold a declared workflow, and keep the answers apart."""
    from scripts import team_launcher as launcher

    config_declares = False
    config_unreadable = ""
    try:
        config_declares = bool(launcher._load_json(config_path).get("workflow"))
    except (SystemExit, OSError, ValueError) as exc:
        # Deliberately NOT "False". An unreadable config used to be
        # indistinguishable from an exempt tenant, which is the same wrong
        # answer for a completely different reason.
        config_unreadable = f"{config_path} could not be read ({exc})"

    recorded, _problem = launcher.recorded_declared_workflow(config.project)

    plan_declares = False
    non_declarative = False
    try:
        plan = launcher._load_json(config_path.parent / "plan.json")
        plan_declares = bool(plan.get("workflow"))
        non_declarative = str(plan.get("workflow_seed") or "") == launcher.NON_DECLARATIVE_WORKFLOW_SEED
    except (SystemExit, OSError, ValueError):
        plan_declares = False

    reader = board_reader or launcher.read_board_declared_workflow
    document, board_problem = reader(config)
    return launcher.DeclaredWorkflowPresence(
        project=config.project,
        config_declares=config_declares,
        config_unreadable=config_unreadable,
        root_records=recorded is not None,
        plan_declares=plan_declares,
        board_document=document is not None,
        board_problem=board_problem,
        non_declarative_by_design=non_declarative,
    )
