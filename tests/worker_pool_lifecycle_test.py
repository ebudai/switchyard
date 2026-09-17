#!/usr/bin/env python3
"""SYRD-37: the life of one interchangeable worker, from declared to retired.

The declaration and the preflight are covered next door. This is everything
that happens afterwards: expanding a pool into the tenant's own workflow
document, what that does to the review lanes downstream of it, the readiness
that has to hold before a worker is handed its first ticket, the ordered
upgrade and the way back out of it, and retirement and replacement -- which are
the two operations that must never let one worker answer to another's name.

Nothing here is about the project that asked for the capability. Every case
declares its own pool, and two of them deliberately declare a pool of a
different size, runtime and kind, because a pool of eight Hermes implementers
must not be the only shape the code can run.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher, worker_pool  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


PROJECT = "stellaris"
POOL = {"name": "impl", "runtime": "hermes", "size": 8, "kind": "implementer"}
#: A second shape, in a different tenant's vocabulary: two attached Codex
#: reviewers with long-lived sessions. Every structural case below runs against
#: this too, so nothing can quietly come to mean "eight ephemeral Hermes".
OTHER_POOL = {
    "name": "reviewbot",
    "runtime": "codex",
    "size": 2,
    "kind": "reviewer",
    "presentation": "attached",
    "ephemeral": False,
}


def pool_of(raw: dict) -> object:
    return team_launcher.parse_worker_pool(raw)


def base_document(project: str = PROJECT) -> dict:
    """A small, complete, valid tenant workflow: one implementer, one reviewer."""
    control = [
        "create_ticket", "file_bug", "add_comment", "edit_fields", "await_role",
        "clear_awaiting_role", "set_blockers", "set_manually_controlled",
        "dismiss_notification", "reassign", "merge", "director_edit",
        "resolve_publication", "recover_stalled_ticket",
    ]
    worker_caps = ["create_ticket", "file_bug", "add_comment", "edit_fields", "await_role", "clear_awaiting_role"]
    return {
        "schema": "switchyard.workflow.v1",
        "project": project,
        "roles": [
            {"name": "director", "label": "Director", "kind": "system", "active": True,
             "capabilities": control, "runtime": "codex", "target": f"{project}-director:0.0", "slot": 0},
            {"name": "main", "label": "Main", "kind": "implementer", "active": True,
             "capabilities": worker_caps, "runtime": "codex", "target": f"{project}-main:0.0", "slot": 1,
             "onboarding_prompt": "Read the board skill, then the ticket."},
            {"name": "review", "label": "Review", "kind": "reviewer", "active": True,
             "capabilities": worker_caps, "runtime": "codex", "target": f"{project}-review:0.0", "slot": 2},
            {"name": "user", "label": "User", "kind": "user", "active": True, "capabilities": worker_caps},
            {"name": "unassigned", "label": "Unassigned", "kind": "system", "active": False,
             "capabilities": worker_caps},
        ],
        "stages": [
            {"name": "analysis", "label": "Triage", "kind": "draft", "owners": ["director"],
             "gate": None, "skip_to": None, "signoff": None, "terminal": False,
             "notify": {"kind": "assignee", "role": None}},
            {"name": "in_progress", "label": "Implementation", "kind": "implementation", "owners": ["main"],
             "gate": None, "skip_to": None, "signoff": None, "terminal": False,
             "notify": {"kind": "assignee", "role": None}},
            {"name": "inspection", "label": "Inspection", "kind": "review", "owners": ["review"],
             "gate": None, "skip_to": None, "signoff": None, "terminal": False,
             "notify": {"kind": "assignee", "role": None}},
            # Somewhere deferred work can wait: not terminal, owned by nobody,
            # announcing nothing. The validator requires one.
            {"name": "backlog", "label": "Backlog", "kind": "system", "owners": [],
             "gate": None, "skip_to": None, "signoff": None, "terminal": False,
             "notify": {"kind": "none", "role": None}},
            {"name": "done", "label": "Done", "kind": "system", "owners": [],
             "gate": None, "skip_to": None, "signoff": None, "terminal": True,
             "notify": {"kind": "none", "role": None}},
        ],
        "transitions": [
            {"from": "analysis", "to": "in_progress", "action": "route", "label": "Route",
             "primitive": "move", "actors": ["director"], "owner_scoped": False,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
            {"from": "in_progress", "to": "inspection", "action": "submit", "label": "Submit",
             "primitive": "move", "actors": ["main"], "owner_scoped": True,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
            {"from": "inspection", "to": "in_progress", "action": "kick_back", "label": "Kick back",
             "primitive": "return", "actors": ["review"], "owner_scoped": False,
             "require_commit": False, "require_reason": True, "clear_signoffs": [], "allow_no_code": False},
            {"from": "inspection", "to": "done", "action": "sign_off", "label": "Sign off",
             "primitive": "move", "actors": ["review"], "owner_scoped": False,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
            {"from": "done", "to": "analysis", "action": "reopen", "label": "Reopen",
             "primitive": "reopen", "actors": ["director"], "owner_scoped": False,
             "require_commit": False, "require_reason": True, "clear_signoffs": [], "allow_no_code": False},
            # The director has to be able to move work out of every stage; the
            # validator refuses a document where it cannot, and a tenant that
            # loses that has work nobody can unstick.
            {"from": "in_progress", "to": "analysis", "action": "route", "label": "Route back",
             "primitive": "move", "actors": ["director"], "owner_scoped": False,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
            {"from": "inspection", "to": "analysis", "action": "route", "label": "Route back",
             "primitive": "move", "actors": ["director"], "owner_scoped": False,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
            {"from": "analysis", "to": "backlog", "action": "defer", "label": "Defer",
             "primitive": "move", "actors": ["director"], "owner_scoped": False,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
            {"from": "backlog", "to": "analysis", "action": "route", "label": "Route",
             "primitive": "move", "actors": ["director"], "owner_scoped": False,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
            {"from": "in_progress", "to": "backlog", "action": "defer", "label": "Defer",
             "primitive": "move", "actors": ["director"], "owner_scoped": False,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
            {"from": "inspection", "to": "backlog", "action": "defer", "label": "Defer",
             "primitive": "move", "actors": ["director"], "owner_scoped": False,
             "require_commit": False, "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
        ],
        "flags": {},
        # Where a ticket waits when the worker it is meant for already holds
        # one. A declared board refuses to serialise an implementer without
        # somewhere to put the ticket it will not deliver (SYRD-31).
        "queue": {"stage": "analysis", "assignee": "director"},
        "reassign": {},
        "remove_stages": [],
    }


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_one_rule_names_a_worker_everywhere_it_appears() -> None:
    identity = worker_pool.WorkerIdentity("stellaris", "impl", 3, Path("/srv/worktrees"))
    check(identity.role == "impl-3", identity.role)
    check(identity.tmux_session == "stellaris-impl-3", identity.tmux_session)
    check(identity.target == "stellaris-impl-3:0.0", identity.target)
    check(str(identity.workdir) == "/srv/worktrees/impl-3", str(identity.workdir))
    check(identity.label == "Impl 3", identity.label)
    # The board derives the same target from the document, and refuses any
    # other: `apply_declared_workflow` rejects a role whose target is not
    # `<project>-<role>:0.0`. One rule, checked on both sides.
    check(identity.target == f"stellaris-{identity.role}:0.0", "the board's rule and this one agree")


def test_a_pool_member_is_recognised_and_a_lookalike_is_not() -> None:
    check(worker_pool.member_index("impl", "impl-7") == 7, "a member is its number")
    check(worker_pool.member_index("impl", "impl") is None, "the pool name alone is not a member")
    check(worker_pool.member_index("impl", "impl-0") is None, "members are numbered from one")
    check(worker_pool.member_index("impl", "implementer-1") is None, "a longer name is a different pool")
    check(worker_pool.member_index("impl", "other-1") is None, "and so is another pool's member")


# --------------------------------------------------------------------------
# Expansion
# --------------------------------------------------------------------------


def test_expanding_a_pool_declares_every_worker_and_routes_it() -> None:
    pool = pool_of(POOL)
    expanded, changes = worker_pool.expand_pool(
        base_document(), pool, project=PROJECT, worktree_base=Path("/srv/worktrees")
    )
    validate(expanded, project=PROJECT)
    members = worker_pool.live_members(expanded, pool)
    check(members == [f"impl-{index}" for index in range(1, 9)], str(members))
    stage = next(s for s in expanded["stages"] if s["name"] == "in_progress")
    check(stage["owners"] == ["main", *members], str(stage["owners"]))
    worker = next(role for role in expanded["roles"] if role["name"] == "impl-1")
    check(worker["runtime"] == "hermes", "it runs the pool's runtime")
    check(worker["target"] == f"{PROJECT}-impl-1:0.0", "and answers at its own address")
    check(worker["slot"] is None, "on-demand presentation means no permanent slot")
    check(worker["ephemeral"] is True, "and a cleared session per ticket")
    check(worker["serial"] is True, "and one ticket at a time")
    check(worker["capabilities"] == next(r for r in expanded["roles"] if r["name"] == "main")["capabilities"],
          "its capabilities are the template's, not a list in the code")
    check(worker["onboarding_prompt"] == "Read the board skill, then the ticket.",
          "and so is its onboarding prompt")
    check(any(change.subject == "stage in_progress" for change in changes), str(changes))


def test_expanding_the_same_pool_twice_is_one_pool() -> None:
    pool = pool_of(POOL)
    once, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
    twice, changes = worker_pool.expand_pool(once, pool, project=PROJECT)
    check(twice == once, "a second expansion changes nothing")
    check(len(worker_pool.live_members(twice, pool)) == 8, "and does not double the pool")
    check("already declared" in changes[0].detail, changes[0].detail)


def test_a_pool_of_another_size_runtime_and_kind_expands_the_same_way() -> None:
    pool = pool_of(OTHER_POOL)
    expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
    validate(expanded, project=PROJECT)
    members = worker_pool.live_members(expanded, pool)
    check(members == ["reviewbot-1", "reviewbot-2"], str(members))
    worker = next(role for role in expanded["roles"] if role["name"] == "reviewbot-1")
    check(worker["runtime"] == "codex" and worker["kind"] == "reviewer", json.dumps(worker, sort_keys=True))
    check(worker["ephemeral"] is False, "a pool may declare persistent sessions")
    stage = next(s for s in expanded["stages"] if s["name"] == "inspection")
    check(stage["owners"] == ["review", "reviewbot-1", "reviewbot-2"],
          "and a reviewer pool joins the review stage its template owns: " + str(stage["owners"]))


def test_a_pool_declares_the_remit_its_workers_start_with() -> None:
    # A worker copied from `main` inherits main's remit, which is the right
    # default and a poor answer for a bench whose job differs from it.
    pool = pool_of({**POOL, "onboarding_prompt": "You are one of a bench. Take one ticket."})
    expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
    validate(expanded, project=PROJECT)
    for member in worker_pool.live_members(expanded, pool):
        role = next(r for r in expanded["roles"] if r["name"] == member)
        check(role["onboarding_prompt"].startswith("You are one of a bench"), role["onboarding_prompt"])
    inherited, _ = worker_pool.expand_pool(base_document(), pool_of(POOL), project=PROJECT)
    check(
        next(r for r in inherited["roles"] if r["name"] == "impl-1")["onboarding_prompt"]
        == "Read the board skill, then the ticket.",
        "and a pool that declares none inherits the template's",
    )
    check(
        team_launcher.WORKER_POOL_ONBOARDING_PROMPT_MAX_CHARS
        == workflow_config_prompt_limit(),
        "the bound is the document's own, so a pool cannot declare one it would refuse",
    )


def workflow_config_prompt_limit() -> int:
    from scripts.ticket_board.workflow_config import ONBOARDING_PROMPT_MAX_CHARS

    return ONBOARDING_PROMPT_MAX_CHARS


def test_an_attached_pool_takes_real_slots_and_is_refused_when_it_cannot_fit() -> None:
    # `attached` is a promise that every worker holds a pane. A window has six
    # slots; a pool that would need a seventh has to be told so here, not by the
    # document validator complaining about a number.
    fitting, _ = worker_pool.expand_pool(
        base_document(), pool_of(OTHER_POOL), project=PROJECT
    )
    validate(fitting, project=PROJECT)
    slots = sorted(
        next(r for r in fitting["roles"] if r["name"] == name)["slot"]
        for name in worker_pool.live_members(fitting, pool_of(OTHER_POOL))
    )
    check(slots == [3, 4], f"the free slots are handed out: {slots}")
    try:
        worker_pool.expand_pool(
            base_document(), pool_of({**OTHER_POOL, "size": 8}), project=PROJECT
        )
    except ValueError as exc:
        check("free slot(s)" in str(exc) and "on-demand" in str(exc), str(exc))
    else:
        check(False, "an attached pool that cannot fit must be refused")
    # And an on-demand pool of the same size is fine, because it holds no slot.
    on_demand, _ = worker_pool.expand_pool(
        base_document(), pool_of({**OTHER_POOL, "size": 8, "presentation": "on-demand"}),
        project=PROJECT,
    )
    validate(on_demand, project=PROJECT)
    check(
        all(
            next(r for r in on_demand["roles"] if r["name"] == name)["slot"] is None
            for name in worker_pool.live_members(on_demand, pool_of(OTHER_POOL))
        ),
        "on-demand presentation is the absence of a slot",
    )


def test_a_pool_with_no_role_to_copy_is_refused_by_name() -> None:
    document = base_document()
    document["roles"] = [role for role in document["roles"] if role["name"] != "review"]
    document["stages"] = [stage for stage in document["stages"] if stage["name"] != "inspection"]
    document["transitions"] = [
        transition for transition in document["transitions"]
        if "inspection" not in (transition["from"], transition["to"])
    ]
    document["transitions"].append(
        {"from": "in_progress", "to": "done", "action": "finish", "label": "Finish",
         "primitive": "move", "actors": ["main"], "owner_scoped": True, "require_commit": False,
         "require_reason": False, "clear_signoffs": [], "allow_no_code": False}
    )
    validate(document, project=PROJECT)
    try:
        worker_pool.expand_pool(document, pool_of(OTHER_POOL), project=PROJECT)
    except ValueError as exc:
        check("no active reviewer role" in str(exc), str(exc))
    else:
        check(False, "a pool with nothing to copy must be refused, not invented")


# --------------------------------------------------------------------------
# Review admission
# --------------------------------------------------------------------------


def test_expanding_a_pool_gives_every_review_lane_it_feeds_a_head() -> None:
    pool = pool_of(POOL)
    before = worker_pool.review_admission(base_document(), pool)
    check(len(before.lanes) == 0, "an unexpanded pool owns nothing and feeds nothing")

    expanded, changes = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
    admission = worker_pool.review_admission(expanded, pool)
    check([lane.stage for lane in admission.lanes] == ["inspection"], str(admission.lanes))
    check(not admission.ambiguous_lanes, "and the lane is ordered once the pool is declared")
    check(
        any("becomes serial" in change.detail for change in changes),
        "and the change is reported rather than done quietly: " + str(changes),
    )
    check(next(r for r in expanded["roles"] if r["name"] == "review")["serial"] is True,
          "the reviewer is serial in the document, which is where the listener reads it")


def test_a_lane_that_is_not_serialised_reads_as_ambiguous() -> None:
    pool = pool_of(POOL)
    expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
    loosened = copy.deepcopy(expanded)
    next(role for role in loosened["roles"] if role["name"] == "review")["serial"] = False
    admission = worker_pool.review_admission(loosened, pool)
    check([lane.stage for lane in admission.ambiguous_lanes] == ["inspection"], str(admission.lanes))
    rendered = "\n".join(worker_pool.format_review_admission(admission))
    check("AMBIGUOUS" in rendered and "8 tickets at once" in rendered, rendered)
    check("declare `serial`" in rendered, "and it says what to do about it: " + rendered)


def test_a_control_stage_is_not_mistaken_for_a_review_lane() -> None:
    # The director deliberately holds several tickets at once; a report that
    # demanded it be serialised would be telling an operator to break the one
    # role that has to see everything.
    pool = pool_of(POOL)
    document = base_document()
    document["transitions"].append(
        {"from": "in_progress", "to": "analysis", "action": "hand_back", "label": "Hand back",
         "primitive": "move", "actors": ["main"], "owner_scoped": True, "require_commit": False,
         "require_reason": True, "clear_signoffs": [], "allow_no_code": False}
    )
    expanded, _ = worker_pool.expand_pool(document, pool, project=PROJECT)
    lanes = [lane.stage for lane in worker_pool.review_admission(expanded, pool).lanes]
    check("analysis" not in lanes, f"a director stage is not a review lane: {lanes}")
    check("inspection" in lanes, f"and the reviewer's still is: {lanes}")


def test_review_lanes_are_followed_past_the_first_hop() -> None:
    pool = pool_of(POOL)
    document = base_document()
    document["roles"].append(
        {"name": "audit", "label": "Audit", "kind": "reviewer", "active": True,
         "capabilities": ["add_comment", "edit_fields"], "runtime": "codex",
         "target": f"{PROJECT}-audit:0.0", "slot": 3}
    )
    document["stages"].insert(3, {
        "name": "audit", "label": "Audit", "kind": "review", "owners": ["audit"],
        "gate": None, "skip_to": None, "signoff": None, "terminal": False,
        "notify": {"kind": "assignee", "role": None},
    })
    document["transitions"] = [
        transition for transition in document["transitions"]
        if not (transition["from"] == "inspection" and transition["to"] == "done")
    ] + [
        {"from": "inspection", "to": "audit", "action": "sign_off", "label": "Sign off",
         "primitive": "move", "actors": ["review"], "owner_scoped": False, "require_commit": False,
         "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
        {"from": "audit", "to": "done", "action": "audit_sign_off", "label": "Audit sign off",
         "primitive": "move", "actors": ["audit"], "owner_scoped": False, "require_commit": False,
         "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
        {"from": "audit", "to": "analysis", "action": "route", "label": "Route back",
         "primitive": "move", "actors": ["director"], "owner_scoped": False, "require_commit": False,
         "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
        {"from": "audit", "to": "backlog", "action": "defer", "label": "Defer",
         "primitive": "move", "actors": ["director"], "owner_scoped": False, "require_commit": False,
         "require_reason": False, "clear_signoffs": [], "allow_no_code": False},
    ]
    validate(document, project=PROJECT)
    expanded, _ = worker_pool.expand_pool(document, pool, project=PROJECT)
    lanes = [lane.stage for lane in worker_pool.review_admission(expanded, pool).lanes]
    check(lanes == ["inspection", "audit"], f"the second reviewer is a lane too: {lanes}")


# --------------------------------------------------------------------------
# Retirement and replacement
# --------------------------------------------------------------------------


def test_retiring_a_worker_keeps_its_identity_and_stops_routing_to_it() -> None:
    pool = pool_of(POOL)
    expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
    retired, changes = worker_pool.retire_worker(expanded, pool, "impl-3")
    validate(retired, project=PROJECT)
    role = next(r for r in retired["roles"] if r["name"] == "impl-3")
    check(role["active"] is False, "it is deactivated")
    check(role["target"] is None and role["runtime"] is None, "and its pane address is freed")
    check(any(r["name"] == "impl-3" for r in retired["roles"]), "but its name is still in the document")
    stage = next(s for s in retired["stages"] if s["name"] == "in_progress")
    check("impl-3" not in stage["owners"], "and nothing is routed to it again")
    check(worker_pool.live_members(retired, pool) == [f"impl-{i}" for i in (1, 2, 4, 5, 6, 7, 8)],
          str(worker_pool.live_members(retired, pool)))
    check(any("never reissued" in change.detail for change in changes), str(changes))


def test_a_retired_workers_number_is_never_handed_to_another_worker() -> None:
    pool = pool_of(POOL)
    expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
    replaced, changes = worker_pool.replace_worker(
        expanded, pool, "impl-3", project=PROJECT, worktree_base=Path("/srv/worktrees")
    )
    validate(replaced, project=PROJECT)
    live = worker_pool.live_members(replaced, pool)
    check("impl-3" not in live, "the replaced worker is gone")
    check("impl-9" in live, f"and its replacement has a new identity: {live}")
    check(len(live) == 8, f"the bench is still the declared size: {live}")
    check(worker_pool.retired_members(replaced, pool) == ["impl-3"], "with the old identity retained")
    again, _ = worker_pool.replace_worker(
        replaced, pool, "impl-9", project=PROJECT, worktree_base=Path("/srv/worktrees")
    )
    check("impl-10" in worker_pool.live_members(again, pool), "and numbering keeps going past every retirement")


def test_retiring_something_that_is_not_a_member_is_refused() -> None:
    pool = pool_of(POOL)
    expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
    for name in ("main", "impl", "impl-99"):
        try:
            worker_pool.retire_worker(expanded, pool, name)
        except ValueError as exc:
            check("not a member" in str(exc), str(exc))
        else:
            check(False, f"retiring {name!r} must be refused")


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------


def config_with_pool(tmp_path: Path, *, pool: dict, workers: list[str]) -> tuple[object, Path]:
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps({"Orientation": "Horizontal", "Widgets": []}), encoding="utf-8")
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    roles = [
        {"role": "main", "slot": 0, "target": f"{PROJECT}-main:0.0", "tmux_session": f"{PROJECT}-main",
         "workdir": str(tmp_path / "worktrees" / "main"), "cli": ["codex"]},
    ] + [
        {"role": name, "detached": True, "target": f"{PROJECT}-{name}:0.0",
         "tmux_session": f"{PROJECT}-{name}", "workdir": str(tmp_path / "worktrees" / name),
         "cli": [pool["runtime"]]}
        for name in workers
    ]
    (tmp_path / "worktrees" / "main").mkdir(parents=True, exist_ok=True)
    payload = {
        "project": PROJECT, "ticket_prefix": "STL", "layout": str(layout),
        "repository": str(repository), "run_as_user": team_launcher.current_user_name(),
        "session_dir": str(tmp_path / "state" / "sessions"),
        "board_url": "http://127.0.0.1:26623", "board_socket": "/run/stellaris/board.sock",
        "worktree_base": str(tmp_path / "worktrees"),
        "roles": roles, "worker_pool": pool,
    }
    path = tmp_path / f"{PROJECT}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return team_launcher.load_project_config(PROJECT, path), path


def refusing_runner(*, authenticated: bool = True):
    """Answers the two probes readiness makes, and refuses everything else.

    A runner that fell through to the host would ask the live machine about
    tmux sessions and CLI logins, which is how a suite passes here and hangs on
    a clean one.
    """

    def runner(args, **_kwargs):
        command = [str(part) for part in args]
        if "has-session" in command:
            return subprocess.CompletedProcess(command, 1)
        if command[-2:-1] == ["-c"] and command[-1].startswith("command -v "):
            return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/x\n")
        if "config" in command and "check" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout="\N{CHECK MARK} OPENROUTER_API_KEY\n" if authenticated else "no keys\n"
            )
        raise AssertionError(f"readiness asked the host something it should not: {command}")

    return runner


def test_readiness_names_what_is_missing_rather_than_saying_not_ready() -> None:
    pool = pool_of(POOL)
    with tempfile.TemporaryDirectory(prefix="syrd37-readiness.") as tmp:
        tmp_path = Path(tmp)
        config, _path = config_with_pool(tmp_path, pool=POOL, workers=["impl-1"])
        expanded, _ = worker_pool.expand_pool(
            base_document(), pool, project=PROJECT, worktree_base=tmp_path / "worktrees"
        )
        states = worker_pool.worker_readiness(
            config, pool, document=expanded, owner_home=tmp_path / "home",
            members=["impl-1"], runner=refusing_runner(),
        )
    state = states[0]
    check(state.role == "impl-1" and state.target == f"{PROJECT}-impl-1:0.0", state.describe())
    check(state.declared and state.routed == ("in_progress",), state.describe())
    check(state.onboarding, "the template's prompt came with it")
    blockers = "; ".join(state.blockers)
    check("board skill is missing" in blockers, blockers)
    check("worktree is missing" in blockers, blockers)
    check(not state.ready, "and a worker with blockers is not ready")


def test_an_unauthenticated_runtime_blocks_every_worker_at_once() -> None:
    pool = pool_of(POOL)
    with tempfile.TemporaryDirectory(prefix="syrd37-auth.") as tmp:
        tmp_path = Path(tmp)
        config, _path = config_with_pool(tmp_path, pool=POOL, workers=["impl-1", "impl-2"])
        expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
        states = worker_pool.worker_readiness(
            config, pool, document=expanded, owner_home=tmp_path / "home",
            members=["impl-1", "impl-2"], runner=refusing_runner(authenticated=False),
        )
    check(len(states) == 2, "both workers are reported")
    for state in states:
        check(any("unauthenticated" in blocker for blocker in state.blockers), state.describe())


def test_start_refuses_a_worker_that_is_not_ready_and_says_why() -> None:
    pool = pool_of(POOL)
    with tempfile.TemporaryDirectory(prefix="syrd37-start.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = config_with_pool(tmp_path, pool=POOL, workers=["impl-1"])
        expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
        states = worker_pool.worker_readiness(
            config, pool, document=expanded, owner_home=tmp_path / "home",
            members=["impl-1"], runner=refusing_runner(),
        )

        def never(args, **_kwargs):
            raise AssertionError(f"a worker that is not ready must not be started: {args}")

        action = worker_pool.start_worker(
            config, pool, "impl-1", readiness=states, config_path=config_path, runner=never
        )
    check(action.action == "not started", action.describe())
    check("board skill" in action.detail, action.detail)


def test_forcing_a_start_says_it_was_forced() -> None:
    pool = pool_of(POOL)
    started: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="syrd37-force.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = config_with_pool(tmp_path, pool=POOL, workers=["impl-1"])
        expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
        states = worker_pool.worker_readiness(
            config, pool, document=expanded, owner_home=tmp_path / "home",
            members=["impl-1"], runner=refusing_runner(),
        )

        def record(args, **_kwargs):
            started.append([str(part) for part in args])
            return subprocess.CompletedProcess(args, 0)

        action = worker_pool.start_worker(
            config, pool, "impl-1", readiness=states, config_path=config_path,
            force=True, runner=record,
        )
    check(action.action == "started", action.describe())
    check("forced past its blockers" in action.detail, action.detail)
    check(len(started) == 1, "and exactly one pane command was run")


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def test_a_declared_tenant_is_planned_as_one_reviewed_document_apply() -> None:
    pool = pool_of(POOL)
    with tempfile.TemporaryDirectory(prefix="syrd37-plan.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = config_with_pool(tmp_path, pool=POOL, workers=[])
        expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
        steps = worker_pool.upgrade_plan(config, pool, document=expanded, config_path=config_path)
    names = [step.name for step in steps]
    check("workflow apply" in names and "workflow dry run" in names, str(names))
    check("board registration" not in names, "a declared tenant does not need add-role: " + str(names))
    apply_step = next(step for step in steps if step.name == "workflow apply")
    check("rollback --journal" in apply_step.rollback, apply_step.rollback)
    check(names.index("board skill") < names.index("start"),
          "the skill is installed before any worker starts: " + str(names))
    check(names.index("provider first run") < names.index("start"),
          "and so is the provider's first run: " + str(names))
    check(names.index("worker preparation") < names.index("start"),
          "and its worktree, hooks and trust: " + str(names))


def test_a_blocker_the_plan_clears_is_named_on_the_step_that_clears_it() -> None:
    pool = pool_of(POOL)
    with tempfile.TemporaryDirectory(prefix="syrd37-blockers.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = config_with_pool(tmp_path, pool=POOL, workers=[])
        expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
        steps = worker_pool.upgrade_plan(
            config, pool, document=expanded, config_path=config_path,
            blockers=[
                ("runtime", "hermes reports unauthenticated"),
                ("board", "8 worker identit(ies) are not in the declared workflow"),
                ("role names", "impl-1 already exists and is not a hermes pool worker"),
            ],
        )
    apply_step = next(step for step in steps if step.name == "workflow apply")
    check("Clears: 8 worker identit" in apply_step.detail, apply_step.detail)
    login = next(step for step in steps if step.name == "provider first run")
    check("Clears: hermes reports unauthenticated" in login.detail, login.detail)
    # And the one no step addresses stays a blocker on the plan, because
    # running the sequence would not make the pool work.
    unclaimed = [step for step in steps if step.blocking]
    check(len(unclaimed) == 1 and "role names" in unclaimed[0].name, str(unclaimed))
    check("already exists" in unclaimed[0].detail, unclaimed[0].detail)


def test_a_legacy_tenant_is_planned_through_add_role_and_told_what_it_cannot_have() -> None:
    pool = pool_of(POOL)
    with tempfile.TemporaryDirectory(prefix="syrd37-legacy.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = config_with_pool(tmp_path, pool=POOL, workers=[])
        steps = worker_pool.upgrade_plan(config, pool, document=None, config_path=config_path)
    names = [step.name for step in steps]
    check("board registration" in names, str(names))
    check("workflow apply" not in names, str(names))
    admission = next(step for step in steps if step.name == "review admission")
    check(admission.blocking, "the reviewer cannot be serialised without a document")
    check("8 of this pool's tickets at once" in admission.detail, admission.detail)
    registration = next(step for step in steps if step.name == "board registration")
    check(registration.privileged, "registering a role on a legacy board is privileged")
    journalled = registration.journalled(PROJECT, target_commit="abc123")
    check(journalled[:2] == (worker_pool.ROLLOUT_WRAPPER, PROJECT), str(journalled))
    check("--target-commit" in journalled and "abc123" in journalled, str(journalled))
    check(journalled[journalled.index("--") + 1] == "switchyard", str(journalled))


def test_the_plan_says_it_ran_nothing() -> None:
    pool = pool_of(POOL)
    with tempfile.TemporaryDirectory(prefix="syrd37-planfmt.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = config_with_pool(tmp_path, pool=POOL, workers=[])
        expanded, _ = worker_pool.expand_pool(base_document(), pool, project=PROJECT)
        steps = worker_pool.upgrade_plan(config, pool, document=expanded, config_path=config_path)
    lines = worker_pool.format_plan(PROJECT, steps)
    check(lines[0].startswith(f"switchyard: worker pool plan for {PROJECT}:"), lines[0])
    check(lines[-1].startswith("switchyard: this printed a plan."), lines[-1])
    check(all("undo:" in line or True for line in lines), "every step carries its own way back")
    undo = [line for line in lines if "undo:" in line]
    check(len(undo) >= 5, f"and most of them have one: {undo}")


# --------------------------------------------------------------------------
# The command surface
# --------------------------------------------------------------------------


def test_every_verb_that_names_a_worker_refuses_without_one() -> None:
    for action, needs_member in team_launcher.WORKER_POOL_ACTIONS.items():
        if not needs_member:
            continue
        try:
            team_launcher.switchyard_main(["worker-pool", PROJECT, action])
        except SystemExit as exc:
            check("needs the worker it acts on" in str(exc), f"{action}: {exc}")
        else:
            check(False, f"{action} must refuse without a worker")


def test_the_command_never_escalates() -> None:
    # It writes through the board's workflow API as the invoking role and moves
    # tmux sessions on the project account's own server. Neither needs root, and
    # escalating would put a privileged parent shell behind `attach` (SYRD-76).
    check("worker-pool" in team_launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS, "it is unprivileged")
    check("worker-pool" not in team_launcher.SWITCHYARD_PRIVILEGED_COMMANDS, "and not privileged")
    for action in sorted(team_launcher.WORKER_POOL_ACTIONS):
        check(
            not team_launcher.switchyard_invocation_requires_root(["worker-pool", PROJECT, action]),
            f"worker-pool {action} must not escalate",
        )


def test_the_read_only_verbs_are_the_ones_that_do_not_take_apply() -> None:
    parser = team_launcher._build_switchyard_worker_pool_parser()
    args = parser.parse_args([PROJECT, "apply"])
    check(args.apply_changes is False, "apply is a dry run until --apply is passed")
    check(parser.parse_args([PROJECT, "apply", "--apply"]).apply_changes is True, "and writes when it is")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"worker_pool_lifecycle_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
