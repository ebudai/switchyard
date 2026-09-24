#!/usr/bin/env python3
"""SYRD-240: composing and checking a declared workflow for a legacy tenant.

Live UAT on mefp stopped because its plan declares no workflow, so
`adopt-workflow` had nothing to adopt. `propose_legacy_workflow_adoption`
composes one from ROOT's plan and refuses to propose it unless the running
board corroborates it. These cases are about that corroboration and the
composer's own promises; whether the document BEHAVES like the board is
`legacy_workflow_equivalence_test`'s job, against real PostgreSQL.
"""

from __future__ import annotations

import copy
import json
import signal
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import team_launcher as tl  # noqa: E402
from ticket_board import legacy_workflow as lw  # noqa: E402
from ticket_board import project_provision as pv  # noqa: E402
from ticket_board.workflow_config import LEGACY_ASSIGNEE_SCOPED_OPERATIONS, validate  # noqa: E402

CHECKS = 0
PROJECT = "mefp"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def mefp_plan(**overrides):
    kwargs = dict(
        project=PROJECT, project_name="MEFP", owner_user="stellaris-agent",
        owner_home=Path("/home/stellaris-agent"), source_repo=ROOT,
        # As live MEFP's board shows it: a designer-owned draft stage, main and
        # ops implementing, audit reviewing (read from /api/workflow).
        implementer_roles=("main", "ops"), include_designer=True, include_audit=True,
    )
    kwargs.update(overrides)
    return pv.build_plan(**kwargs)


_CONFIG_DIR = tempfile.TemporaryDirectory(prefix="syrd262-config.")

#: MEFP's four panes as the User runs them: the Director and Ops on Codex,
#: which the shipped example -- and so the first composer -- said were Claude.
MEFP_PANES = (("director", "codex"), ("main", "codex"), ("ops", "codex"), ("audit", "claude"))


def mefp_config(panes=MEFP_PANES):
    """A real tenant configuration, loaded the way the adopt command loads it."""
    path = Path(_CONFIG_DIR.name) / f"{PROJECT}-{abs(hash(panes))}.json"
    path.write_text(json.dumps({
        "project": PROJECT,
        "board_url": "http://127.0.0.1:1/",
        "board_socket": "/nonexistent/mefp-ticket-board.sock",
        "role_state_isolation": True,
        "roles": [
            {"role": role, "cli": [f"/usr/local/bin/{cli}"], "slot": slot,
             "workdir": f"{_CONFIG_DIR.name}/worktrees/{role}"}
            for slot, (role, cli) in enumerate(panes)
        ],
    }), encoding="utf-8")
    return tl.load_project_config(PROJECT, path)


def registered_binding(role) -> tuple[str, str]:
    """What this role's pane actually registers with: read from its launch command."""
    command = tl.cli_command_for_role(role, session_dir=Path(_CONFIG_DIR.name) / "sessions")
    return command[command.index("--runtime") + 1], command[command.index("--target") + 1]


def compose(plan=None, config=None):
    plan = plan or mefp_plan()
    config = config or mefp_config()
    return lw.compose_legacy_workflow(
        plan, canonical=lw.load_canonical(ROOT),
        stage_seeds=pv.project_workflow_stages(plan),
        transition_seeds=pv.project_workflow_transitions(plan),
        panes={role.role: tl.role_pane_declaration(role) for role in config.roles},
    )


def live_columns(plan=None):
    """What `/api/board` answers for a board seeded from this plan."""
    plan = plan or mefp_plan()
    return [{"key": s.name, "label": s.display_label} for s in pv.project_workflow_stages(plan)]


def no_workflow(_config):
    return None, "the board is running no declared workflow"


def config():
    return mefp_config()


def propose(plan=None, *, board=no_workflow, columns=None, config_override=None):
    plan = plan or mefp_plan()
    cols = live_columns(plan) if columns is None else columns
    return tl.propose_legacy_workflow_adoption(
        PROJECT, plan, config_override or config(),
        board_reader=board,
        columns_reader=lambda _c: (cols, "") if cols is not None else (None, "unreachable"),
    )


# -- the composer's promises -------------------------------------------------


def test_the_composed_document_validates_and_reproduces_the_seeded_stages() -> None:
    declared = compose()
    document = validate(copy.deepcopy(declared.document), project=PROJECT)
    seeded = [s.name for s in pv.project_workflow_stages(mefp_plan())]
    declared_stages = [s["name"] for s in document["stages"] if s["name"] != "backlog"]
    check(declared_stages == seeded, f"every seeded stage, in order: {declared_stages}")


def test_a_tenant_with_a_designer_is_representable_too() -> None:
    """The DEFAULT default-project plan has a designer, and it owns `draft`.

    The first composer copied role activeness from the shipped example, which
    marks `designer` inactive, so this -- the most common shape -- failed the
    validator. mefp has no designer, which is why every mefp case passed.
    Whether a role is active is authority, and it is now read from the seed.
    """
    plan = pv.build_plan(
        project=PROJECT, project_name="MEFP", owner_user="o",
        owner_home=Path("/h"), source_repo=ROOT,
    )
    draft = next(s for s in pv.project_workflow_stages(plan) if s.name == "draft")
    check("designer" in draft.owner_roles, f"this plan's draft belongs to designer: {draft}")
    # A tenant that has a designer runs a designer pane; and this plan's
    # implementation stage is shared by main and app.
    panes = MEFP_PANES + (("designer", "claude"), ("app", "codex"))
    declared = compose(plan, config=mefp_config(panes))
    document = validate(copy.deepcopy(declared.document), project=PROJECT)
    designer = next(r for r in document["roles"] if r["name"] == "designer")
    check(designer["active"] is True, f"so designer is active in its document: {designer}")


def test_each_pane_is_declared_as_exactly_what_it_registers() -> None:
    """SYRD-262: the board hides a role whose declaration and registration differ."""
    config = mefp_config()
    declared = {r["name"]: r for r in compose(config=config).document["roles"]}
    for role in config.roles:
        registered = registered_binding(role)
        check(
            (declared[role.role]["runtime"], declared[role.role]["target"]) == registered,
            f"{role.role} is declared as {declared[role.role].get('runtime')}/"
            f"{declared[role.role].get('target')} and registers as {registered}",
        )
    check(declared["director"]["runtime"] == "codex" and declared["ops"]["runtime"] == "codex",
          "MEFP's Director and Ops are Codex, as they run")
    for role in config.roles:
        check(declared[role.role]["slot"] == role.slot,
              f"{role.role} is shown where its tenant puts it: {declared[role.role]['slot']} vs {role.slot}")
    for name, role in declared.items():
        if name not in {r.role for r in config.roles}:
            check(role.get("runtime") is None and role.get("target") is None and role.get("slot") is None,
                  f"{name} runs no pane here, so it is bound to none: {role}")


def test_a_pane_the_workflow_would_not_declare_is_disclosed() -> None:
    config = mefp_config(MEFP_PANES + (("inspector", "claude"),))
    declared = compose(config=config)
    check(any("pane role inspector" in line for line in declared.differences),
          f"a pane the board would ignore is said out loud: {declared.differences}")
    proposal = propose(config_override=config)
    check("changes: pane role inspector" in "\n".join(proposal.accounting),
          f"and shown before the digest is vouched: {proposal.accounting}")


def test_a_stage_whose_only_owners_have_no_pane_is_declared_silent() -> None:
    """MEFP's `draft` belongs to a designer it runs no pane for."""
    declared = compose()
    draft = next(s for s in declared.document["stages"] if s["name"] == "draft")
    check(draft["owners"] == ["designer"], f"ownership is unchanged: {draft}")
    check(draft["notify"]["kind"] == "none", f"and it notifies nobody: {draft['notify']}")
    # Legacy queued no designer notice either (legacy_workflow_equivalence_test,
    # "route into draft for the designer"), so it is not claimed as a change.
    check(not any("draft" in line for line in declared.differences),
          f"and it is not claimed as a change, because it is not one: {declared.differences}")
    validate(copy.deepcopy(declared.document), project=PROJECT)
    check(True, "and the document validates")


def test_a_stage_shared_with_a_paneless_owner_is_refused_not_silenced() -> None:
    plan = pv.build_plan(
        project=PROJECT, project_name="MEFP", owner_user="o",
        owner_home=Path("/h"), source_repo=ROOT,
    )
    try:
        compose(plan, config=mefp_config(MEFP_PANES + (("designer", "claude"),)))
        check(False, "a stage shared by main and a paneless app was silenced or guessed")
    except lw.LegacyWorkflowRefused as exc:
        check("in_progress" in str(exc) and "app" in str(exc), f"names the stage and role: {exc}")


def test_every_authority_field_is_read_from_the_seed() -> None:
    """Owners, gates, skips, sign-offs and terminality are copied, not chosen."""
    declared = compose()
    by_name = {s["name"]: s for s in declared.document["stages"]}
    for seed in pv.project_workflow_stages(mefp_plan()):
        stage = by_name[seed.name]
        check(stage["owners"] == list(seed.owner_roles), f"{seed.name} owners")
        check(stage["gate"] == seed.entry_gate_field, f"{seed.name} gate")
        check(stage["skip_to"] == seed.gate_skip_to, f"{seed.name} skip_to")
        check(stage["signoff"] == seed.exit_signoff_field, f"{seed.name} signoff")
        check(stage["terminal"] == bool(seed.is_terminal), f"{seed.name} terminal")


def test_assignee_scoped_operations_are_never_widened() -> None:
    """The authority leak the differential test found, pinned statically too."""
    declared = compose()
    for transition in declared.document["transitions"]:
        if transition["action"] in LEGACY_ASSIGNEE_SCOPED_OPERATIONS:
            check(
                transition["owner_scoped"] is True,
                f"{transition['action']} stays assignee-only: {transition}",
            )


def test_no_live_ticket_is_rewritten_as_a_side_effect() -> None:
    """`reassign` is an instruction to move tickets, not a description."""
    check(compose().document["reassign"] == {}, "the document reassigns nothing")


def test_every_semantic_the_table_calls_shipped_is_the_shipped_one() -> None:
    """`shipped` is a promise that nothing was invented. Keep it."""
    shipped = lw.load_canonical(ROOT)
    for action, meaning in lw.ACTION_SEMANTICS.items():
        if meaning.source != "shipped":
            continue
        found = {
            (t["primitive"], t["require_commit"], t["require_reason"],
             tuple(t.get("clear_signoffs") or []))
            for t in shipped["transitions"] if t["action"] == action
        }
        check(
            found == {(meaning.primitive, meaning.require_commit, meaning.require_reason,
                       meaning.clear_signoffs)},
            f"{action} is exactly what the shipped document says: table "
            f"{(meaning.primitive, meaning.require_commit, meaning.require_reason, meaning.clear_signoffs)}"
            f" vs shipped {found}",
        )


def test_an_action_nobody_named_is_a_refusal_not_a_default() -> None:
    plan = mefp_plan()
    trimmed = {k: v for k, v in lw.ACTION_SEMANTICS.items() if k != "start_work"}
    try:
        lw.compose_legacy_workflow(
            plan, canonical=lw.load_canonical(ROOT),
            stage_seeds=pv.project_workflow_stages(plan),
            transition_seeds=pv.project_workflow_transitions(plan),
            panes={role.role: tl.role_pane_declaration(role) for role in mefp_config().roles},
            semantics=trimmed,
        )
        check(False, "an unnamed action was given a primitive")
    except lw.LegacyWorkflowRefused as exc:
        check("start_work" in str(exc), f"it names the action: {exc}")
        check("guessed" in str(exc), f"and why that is refused: {exc}")


def test_the_unreachable_legacy_row_is_omitted_with_its_evidence() -> None:
    declared = compose()
    omitted = " ".join(declared.excluded)
    check("director_dat_kick_back dat -> analysis" in omitted, omitted)
    check("in_progress" in omitted, f"with the line of SQL that proves it: {omitted}")
    check(
        not any(t["action"] == "director_dat_kick_back" and t["to"] == "analysis"
                for t in declared.document["transitions"]),
        "and the capability it would have granted is not declared",
    )


def test_a_gated_sign_off_is_declared_to_one_destination() -> None:
    """Two destinations made the sign-off ambiguous, and Audit could not sign off."""
    declared = compose()
    sign_offs = [t for t in declared.document["transitions"] if t["action"] == "audit_sign_off"]
    check(len(sign_offs) == 1, f"one declared destination: {sign_offs}")
    check(sign_offs[0]["to"] == "dat", f"to the gated stage: {sign_offs[0]['to']}")
    dat = next(s for s in declared.document["stages"] if s["name"] == "dat")
    check(dat["skip_to"] == "director_review", f"whose own gate carries it on: {dat}")


# -- the proposal, and what it refuses ---------------------------------------


def test_a_default_project_tenant_with_no_workflow_gets_a_proposal() -> None:
    proposal = propose()
    check(proposal.problems == (), f"nothing in the way: {proposal.problems}")
    check(proposal.document is not None, "a document is proposed")
    check(len(proposal.digest) == 64, f"with its digest: {proposal.digest}")
    check(proposal.adoptable, "and it is adoptable")
    joined = "\n".join(proposal.accounting)
    check("changes: implementer_kick_back" in joined, f"the blocker change is shown: {joined}")
    check("adds: stage backlog" in joined, f"the addition is shown: {joined}")
    check("omits: director_dat_kick_back" in joined, f"the omission is shown: {joined}")
    for false_claim in ("user_reopen", "route done", "route cancelled"):
        check(
            false_claim not in joined,
            f"and nothing the differential test showed to be identical: {false_claim}",
        )


def test_a_board_already_running_a_workflow_is_left_alone() -> None:
    proposal = propose(board=lambda _c: ({"schema": "x"}, ""))
    check(proposal.document is None, "nothing is proposed")
    check("already running a declared workflow" in " ".join(proposal.problems),
          f"{proposal.problems}")


def test_an_unreachable_board_is_not_read_as_an_empty_one() -> None:
    proposal = propose(board=lambda _c: (None, "the board's workflow could not be read: refused"))
    check(proposal.document is None, "nothing is proposed on an unknown")
    check("could not be read" in " ".join(proposal.problems), f"{proposal.problems}")


def test_a_board_that_drifted_from_its_seed_is_refused() -> None:
    drifted = live_columns()
    drifted[1] = {"key": drifted[1]["key"], "label": "Renamed Triage"}
    proposal = propose(columns=drifted)
    check(proposal.document is None, "a drifted board gets no proposal")
    check("drifted" in " ".join(proposal.problems), f"{proposal.problems}")

    reordered = live_columns()
    reordered[1], reordered[2] = reordered[2], reordered[1]
    check(propose(columns=reordered).document is None, "nor does a reordered one")

    extra = live_columns() + [{"key": "inspection", "label": "Inspection"}]
    check(propose(columns=extra).document is None, "nor one with a stage its seed lacks")


def test_another_seed_is_refused_rather_than_composed_for() -> None:
    proposal = propose(mefp_plan(project="pgu"))
    check(proposal.document is None, "only default-project is composed for")
    check("pgu-full" in " ".join(proposal.problems), f"{proposal.problems}")


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("legacy_workflow_adoption_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"legacy_workflow_adoption_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
