#!/usr/bin/env python3
"""SYRD-843: the optional plan gate -- a designer-owned, audited, User-approved three-level plan the Director scopes from.

* **Composition** (`plan_gate`): an existing owner, or a new Designer made an Implementation owner with `plan_change`
  as its routing scope; every refusal; off and on; every order with the documentation gate, Router, the Publisher and
  the research stage (120), each turned off and on again; each broken shape refused; the index parser, rule by rule.
* **The board's own validator**, on a schema.sql board and on main's tree upgraded with this tree's migrations.
* **The flow on the migrated board** -- real HTTP server and listener, a git repository as the trusted commit cache,
  the documentation gate (Director-owned), Router and the research stage also on:
  - before any plan: routing needs a deliverable or an unplanned reason, and no deliverable can be named;
  - a plan ticket through Audit, User Review and Final Sign-Off: its candidate changes only the plan and the decision
    log, with a valid index; it is closed only with the User's acceptance; its closing commit's index becomes the
    approved plan;
  - deliverables: an unknown one refused, naming the plan's; routing into an undetailed milestone refused, re-plan
    due; a research ticket may name one;
  - the milestone-complete notice to the Director and the plan owner, once;
  - scope changes: a plan ticket opened for the owner, then extended, each waiting ticket blocked on it;
  - findings: a non-plan candidate that changes the plan, and a plan over the size limit, once per commit; a
    documentation commit that changes the plan refused;
  - plan_status; disable refused while a plan ticket is open, accepted after.
* **The real `ticket-board-write`**, and **`switchyard plan-gate`** against a board and a real projection.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

from documentation_gate_board_test import Store, example, last_comment, row, short  # noqa: E402
from router_role_board_test import Board, fixture, temporary_cluster  # noqa: E402
from router_role_board_test import to as _to  # noqa: E402
from scripts.ticket_board import plan_gate as pg  # noqa: E402
from scripts.ticket_board import research_stage as rs  # noqa: E402
from scripts.ticket_board.documentation_gate import candidate_ref, with_documentation_gate, without_documentation_gate  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher, without_publisher  # noqa: E402
from scripts.ticket_board.router_setup import with_router, without_router  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

CHECKS = 0
SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
MIGRATION = "scripts/ticket_board/migrations/pgu1052_syrd843_plan_gate.sql"
PLAN, LOG = "docs/implementation-plan.md", "docs/decision-log.md"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def refusal(fn) -> str:
    try:
        fn()
    except (ValueError, SystemExit) as exc:
        return str(exc)
    return ""


def to(batch, role: str) -> list[str]:
    return [m for m in _to(batch, role) if m != "/clear"]


def role(doc: dict, name: str) -> dict:
    return next(r for r in doc["roles"] if r["name"] == name)


def plan_text(milestones: list, *, filler: int = 0, blocks: int = 1) -> str:
    index = "```plan-index\n" + json.dumps({"milestones": milestones}, indent=1) + "\n```\n"
    return "# Plan\n\n" + index * blocks + "".join(f"Line {i}.\n" for i in range(filler))


M1 = {"id": "M1", "title": "Ingest", "detailed": True,
      "deliverables": [{"id": "D1.1", "title": "Reader"}, {"id": "D1.2", "title": "Indexer"}]}
M2 = {"id": "M2", "title": "Query", "detailed": False, "deliverables": [{"id": "D2.1", "title": "Search"}]}


def test_the_shapes_compose_and_turn_off() -> None:
    doc = pg.with_plan_gate(copy.deepcopy(SHIPPED), owner="main")
    check(doc["plan"] == {"owner": "main", "file": PLAN, "decision_log": LOG, "max_lines": 1500}
          and doc["flags"]["plan_change"] == {"kind": "gate", "default": False}
          and [s["name"] for s in doc["stages"]] == [s["name"] for s in SHIPPED["stages"]],
          f"an Implementation owner owns the plan; the gate adds a flag and a policy, and no stage: {doc['plan']}")
    designed = pg.with_plan_gate(copy.deepcopy(SHIPPED), designer={"runtime": "claude", "template_role": "audit"},
                                 plan_file="plan/PLAN.md", decision_log="plan/DECISIONS.md", max_lines=900)
    designer = role(designed, "designer")
    impl = next(s for s in designed["stages"] if s["kind"] == "implementation")
    check(designer["active"] and designer["kind"] == "draft" and designer["additional_kinds"] == ["implementer"]
          and designer["routing_scope"] == {"flag": "plan_change"} and "designer" in impl["owners"]
          and designer["target"] == "cerulean-designer:0.0" and designer["runtime"] == "claude"
          and "request_publication" not in designer["capabilities"] and designed["plan"]["owner"] == "designer"
          and designed["plan"]["max_lines"] == 900,
          f"a new Designer is a detached draft role that also implements, given only plan tickets by Router: {designer}")
    off = pg.without_plan_gate(designed)
    check("plan" not in off and "plan_change" not in off["flags"] and "routing_scope" not in role(off, "designer"),
          "disable removes the policy, the flag and the designer's routing scope")
    for kwargs, words in (({}, "one of the two"), ({"owner": "main", "designer": {"runtime": "claude"}}, "one of the two"),
                          ({"owner": "audit"}, "does not own Implementation"), ({"owner": "router"}, "that can own the plan"),
                          ({"owner": "user"}, "that can own the plan"), ({"owner": "ghost"}, "that can own the plan"),
                          ({"designer": {"runtime": "emacs"}}, "runtime must be one of"),
                          ({"owner": "main", "plan_file": "../plan.md"}, "repository-relative Markdown"),
                          ({"owner": "main", "plan_file": "plan.txt"}, "repository-relative Markdown"),
                          ({"owner": "main", "plan_file": LOG}, "two files"),
                          ({"owner": "main", "max_lines": 10}, "size limit")):
        said = refusal(lambda: pg.with_plan_gate(copy.deepcopy(SHIPPED), **kwargs))
        check(words in said, f"refused: {kwargs} -> {said}")
    check("already runs the plan gate" in refusal(lambda: pg.with_plan_gate(doc, owner="app")), "enabling twice")
    check("does not run the plan gate" in refusal(lambda: pg.without_plan_gate(copy.deepcopy(SHIPPED))), "disabling off")
    for what, change, words in (
            ("an owner that is not an Implementation owner", lambda d: d["plan"].update(owner="audit"), "Implementation owner"),
            ("a policy with a stray key", lambda d: d["plan"].update(status="M1 done"), "its policy is"),
            ("a plan file outside the repository", lambda d: d["plan"].update(file="/etc/plan.md"), "repository-relative"),
            ("plan_change on by default", lambda d: d["flags"]["plan_change"].update(default=True), "off unless")):
        broken = copy.deepcopy(doc)
        change(broken)
        check(words in refusal(lambda: validate(broken)), f"{what} is refused: {refusal(lambda: validate(broken))}")


def test_it_composes_with_every_other_optional_stage() -> None:
    ops = {"plan": lambda d: pg.with_plan_gate(d, owner="main"),
           "research": lambda d: rs.with_research_stage(d, researcher={"runtime": "claude"}),
           "documentation": lambda d: with_documentation_gate(d, librarian={"runtime": "claude"}),
           "router": lambda d: with_router(d, runtime="claude"), "publisher": with_publisher}
    off = {"plan": pg.without_plan_gate, "research": rs.without_research_stage,
           "documentation": without_documentation_gate, "router": without_router, "publisher": without_publisher}
    seen = 0
    for order in itertools.permutations(ops):
        doc = copy.deepcopy(SHIPPED)
        for name in order:
            doc = ops[name](doc)
        for name in order:
            ops[name](off[name](doc))
        seen += 1
    check(seen == 120, "all 120 orders compose with the research stage, the documentation gate, Router and the "
                       "Publisher, and each turns off and on again in each")
    designed = pg.with_plan_gate(with_router(copy.deepcopy(SHIPPED), runtime="claude"), designer={"runtime": "codex"})
    check(role(designed, "designer")["routing_scope"] == {"flag": "plan_change"}, "with Router, the Designer is scoped")


def test_the_index_is_read_rule_by_rule() -> None:
    check(pg.parse_plan_index(plan_text([M1, M2]))["milestones"][1]["deliverables"] == [{"id": "D2.1", "title": "Search"}],
          "a plan's one index block, in plan order")
    for what, text, words in (
            ("no index", "# Plan\n", "0 `plan-index` blocks"),
            ("two indexes", plan_text([M1], blocks=2), "2 `plan-index` blocks"),
            ("not JSON", "```plan-index\n{milestones}\n```\n", "not JSON"),
            ("no milestones", plan_text([]), "at least one milestone"),
            ("a status field", plan_text([dict(M1, status="done")]), "status, history and rulings never go"),
            ("a duplicate id", plan_text([M1, dict(M2, id="M1")]), "not a unique stable id"),
            ("a deliverable reusing a milestone's id", plan_text([dict(M1, deliverables=[{"id": "M1", "title": "x"}])]),
             "not a unique stable id"),
            ("a detailed milestone with nothing", plan_text([dict(M1, deliverables=[])]), "lists its deliverables"),
            ("a deliverable with a ruling", plan_text([dict(M1, deliverables=[{"id": "D1", "title": "x", "ruling": "y"}])]),
             '{"id", "title"} and nothing else'),
            ("an id with spaces", plan_text([dict(M1, id="M 1")]), "not a unique stable id")):
        said = refusal(lambda: pg.parse_plan_index(text))
        check(words in said, f"{what} is refused: {said}")


def test_the_people_are_taught() -> None:
    director = (ROOT / "docs/onboarding/switchyard-director-guide.md").read_text()
    for words in ("switchyard plan-gate", "set-deliverable", "request-scope-change", "never edit", "plan-index",
                  "Adopting the plan gate on an existing project"):
        check(words in director, f"the Director guide says {words!r}")
    planning = (ROOT / "docs/onboarding/switchyard-plan-guide.md").read_text()
    for words in ("milestone", "deliverable", "acceptance criteria", "plan-index", "decision log", "never",
                  "Audit's plan checklist", "within the size limit"):
        check(words in planning, f"the plan guide says {words!r}")
    check("plan changes go through a plan ticket" in (ROOT / "docs/onboarding/switchyard-librarian-guide.md").read_text(),
          "the Librarian guide says plan changes go through a plan ticket")
    check("plan-gate" in (ROOT / "docs/documentation-map.md").read_text(), "the documentation map finds it")
    from scripts import project_onboarding
    check("switchyard-plan-guide.md" in project_onboarding.SWITCHYARD_ONBOARDING_DOC_NAMES,
          "switchyard new copies the plan guide into every project")


def test_switchyard_new_records_and_composes_the_choice() -> None:
    from scripts import plan_onboarding as onboarding
    from scripts import team_launcher as launcher
    from scripts.new_project_selection import RoleSelection
    from scripts.project_design_payload import project_design_artifact_payload
    from documentation_gate_command_test import dataclass_replace, sample_artifact

    path = Path("/x/p.project.json")
    owners = ["designer", "main", "ops"]
    parse = lambda raw: onboarding.artifact_plan(raw, path=path, owners=owners)  # noqa: E731
    check(parse(None) == () and parse({"owner": "off"}) == () and parse({"owner": "designer"}) == (("owner", "designer"),),
          "no field or off is off; an owner is recorded")
    for raw, words in (({"owner": "audit"}, "names the role that owns the plan"), ({"owner": "main", "file": "x"}, "must be"),
                       ("main", "must be")):
        check(words in refusal(lambda: parse(raw)), f"a malformed record is refused: {raw!r}")
    plan = [RoleSelection("director", "claude"), RoleSelection("main", "codex")]
    rest, record = onboarding.split_role_plan([*plan, RoleSelection(onboarding.GATE_MARKER, "main")])
    check(rest == tuple(plan) and record == (("owner", "main"),), "the choice leaves the role plan as its record")
    check(onboarding.split_role_plan(plan) == (tuple(plan), ()), "nothing chosen is off")
    check("off" in onboarding.summary_line(()) and "owned by main" in onboarding.summary_line(record),
          "each choice is said before anything is created")
    out: list[str] = []
    chosen = onboarding.prompt_plan(["designer", "main"], input_func=lambda _p: "2", print_func=out.append)
    check(chosen == launcher.RoleSelection(onboarding.GATE_MARKER, "designer"), f"the designer may own it: {chosen}")
    check(onboarding.prompt_plan(["main"], input_func=lambda _p: "", print_func=out.append) is None, "the default is off")
    check(any("Plan gate (optional)" in line for line in out), "under a plainly labelled menu")
    payload = project_design_artifact_payload(dataclass_replace(sample_artifact(), plan=(("owner", "main"),)))
    check(payload["project"]["plan"] == {"owner": "main"} and "plan" not in project_design_artifact_payload(
        sample_artifact())["project"], "the artifact records it, and nothing when it is off")


def main_commit() -> str:
    """Main's tree before this change: the parent of the commit that added its migration, or HEAD while uncommitted."""
    adding = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--", MIGRATION],
                            check=True, capture_output=True, text=True).stdout.split()
    return f"{adding[-1]}^" if adding else "HEAD"


def main_tree(into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", main_commit(), "scripts"], check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)
    check(not (into / MIGRATION).exists(), "main's tree has no plan gate migration")
    return into


def flow_document(max_lines: int = 120) -> dict:
    doc = rs.with_research_stage(with_router(with_documentation_gate(example()), runtime="claude"),
                                 researcher={"runtime": "claude"})
    return pg.with_plan_gate(doc, owner="main", max_lines=max_lines)


def sql_validator(cluster) -> None:
    from router_role_board_test import create_roles_once, migrate

    docs = {"an owner": pg.with_plan_gate(copy.deepcopy(SHIPPED), owner="main"),
            "a new Designer": pg.with_plan_gate(copy.deepcopy(SHIPPED), designer={"runtime": "claude"}),
            "everything on": pg.with_plan_gate(rs.with_research_stage(with_publisher(with_router(
                with_documentation_gate(copy.deepcopy(SHIPPED)), runtime="claude")), role="app"),
                designer={"runtime": "codex"}),
            "turned off": pg.without_plan_gate(pg.with_plan_gate(copy.deepcopy(SHIPPED), owner="main"))}
    with tempfile.TemporaryDirectory(prefix="syrd843-main.") as tmp:
        tree = main_tree(Path(tmp) / "main")
        for name, source, upgrade in (("schema_copy", ROOT, False), ("migrated_copy", tree, True)):
            admin = fixture.conninfo(cluster.socket_dir, cluster.port, name)
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", name])
            fixture.psql(admin, (source / "scripts/ticket_board/schema.sql").read_text())
            create_roles_once(admin)
            fixture.psql(admin, (source / "scripts/ticket_board/rbac.sql").read_text())
            if upgrade:
                migrate(admin)
            check(fixture.psql(admin, "SELECT to_regprocedure('ticket_board.request_scope_change(text,text)') IS NOT NULL;")
                  == "t", f"{name}: the scope change function is there")
            for what, doc in docs.items():
                done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"doc={json.dumps(doc)}", "-d", admin],
                                      input="SELECT ticket_board.validate_declared_workflow(:'doc'::jsonb);\n",
                                      capture_output=True, text=True)
                check(done.returncode == 0, f"{name}: the board's validator accepts {what}: {done.stderr[-400:]}")


class Flow:
    """The flow board and its commit cache, with the steps every scenario takes."""

    def __init__(self, cluster, tmp: Path) -> None:
        self.store = Store(tmp / "cache")
        self.base = self.store.commit([], {"README.md": "project\n"}, "base")
        self.store.publish("main", self.base)
        self.board = Board(cluster, "flow", flow_document(), before=main_tree(tmp / "main"))
        self.board.app.commit_git_dirs = (self.store.path,)
        self.board.app.commit_git_dir = self.store.path
        self.board.one_pass()
        self.board.sent.clear()
        self.board.busy.clear()

    def act(self, ticket: str, operation: str, actor: str, **payload) -> str:
        return short(self.board.act(ticket, operation, actor, **payload))

    def seed(self, ticket: str, title: str = "") -> None:
        fixture.seed_postgres_ticket(self.board.admin, ticket, title=title or ticket, state="analysis",
                                     assignee="director")

    def where(self, ticket: str) -> list[str]:
        return self.board.where(ticket)

    def candidate(self, ticket: str, files: dict, parent: str | None = None) -> str:
        commit = self.store.commit([parent or self.base], files, f"{ticket}: work")
        self.store.publish(f"roles/main/{ticket.lower()}", commit)
        return commit

    def pass_(self) -> list:
        self.board.busy.clear()
        batch = self.board.one_pass()
        self.board.busy.clear()
        return batch


def plan_ticket_to_done(f: Flow, t: str, commit: str, *, user: bool = True, close_on: str = "") -> str:
    """A plan ticket, submitted with `commit`, through Audit (and DAT and User Review when `user`) to the Director's
    close on `close_on` (the integration commit; `commit` when not given). Returns "ok", or the step refused and why."""
    steps = [("submit_to_audit", "main", {"commit_hash": commit}), ("audit_sign_off", "audit", {})]
    if user:
        steps += [("set_uat_instructions", "director", {"do_this": "Read the plan.", "you_should_see": "Three levels."}),
                  ("director_dat_sign_off", "director", {}),
                  ("relay_user_sign_off", "director", {"commit_hash": commit, "reason": "The User accepted the plan."})]
    steps.append(("mark_done", "director", {"commit_hash": close_on or commit}))
    for step, actor, payload in steps:
        said = f.act(t, step, actor, **payload)
        if said != "ok":
            return f"{step}: {said} {f.where(t)}"
    return "ok"


def scenarios(cluster) -> None:
    with tempfile.TemporaryDirectory(prefix="syrd843-flow.") as tmp:
        f = Flow(cluster, Path(tmp))
        try:
            before_any_plan(f)
            approved = plan_tickets(f)
            deliverables(f)
            milestone_notice(f)
            scope_changes(f)
            findings(f, approved)
            cli(f)
            disable(f)
        finally:
            f.board.close()


def before_any_plan(f: Flow) -> None:
    t = "PGU-901"
    f.seed(t)
    said = f.act(t, "route", "director", state="in_progress", assignee="app")
    check("names no deliverable" in said and "set-deliverable" in said and f.where(t)[0] == "analysis",
          f"a ticket without a deliverable or a reason is not routed into Implementation: {said}")
    said = f.act(t, "set_ticket_deliverable", "director", deliverable="D1.1")
    check("no plan has been approved yet" in said, f"no deliverable can be named before a plan is approved: {said}")
    for actor in ("main", "audit", "app"):
        check(f.act(t, "set_ticket_deliverable", actor, unplanned_reason="x") != "ok", f"{actor} cannot set it")
    check("one of the two" in f.act(t, "set_ticket_deliverable", "director"), "one of the two is given")
    check(f.act(t, "set_ticket_deliverable", "director", unplanned_reason="An ops one-off: rotate the logs.") == "ok"
          and last_comment(f.board, t).endswith("Unplanned: An ops one-off: rotate the logs."), "unplanned, with why")
    check(f.act(t, "route", "director", state="in_progress", assignee="app") == "ok" and f.where(t) == ["in_progress", "app"],
          "an unplanned ticket is routed like any other")
    check(f.act(t, "cancel", "director") == "ok", "put down")


def plan_tickets(f: Flow) -> str:
    skip = "PGU-910"
    f.seed(skip, "Plan without the User")
    check(f.act(skip, "set_workflow_flags", "director", plan_change=True) == "ok", "a plan ticket, its User gate off")
    check(f.act(skip, "route", "director", state="in_progress", assignee="main") == "ok",
          "a plan ticket needs no deliverable to enter Implementation")
    commit = f.candidate(skip, {PLAN: plan_text([M1, M2])})
    said = plan_ticket_to_done(f, skip, commit, user=False)
    check("the User has not accepted it" in said and f.where(skip)[0] == "director_review",
          f"a plan ticket the User did not accept is not closed: {said} {f.where(skip)}")
    check(f.act(skip, "cancel", "director") == "ok", "put down")

    t = "PGU-911"
    f.seed(t, "The first plan")
    check(f.act(t, "set_workflow_flags", "director", plan_change=True, needs_user_signoff=True) == "ok", "a plan ticket")
    check(f.act(t, "route", "director", state="in_progress", assignee="main") == "ok", "routed to the plan's owner")
    before = f.board.snapshot(t)
    for what, files, words in (
            ("code beside the plan", {PLAN: plan_text([M1, M2]), "src/a.py": "x\n"}, "changes src/a.py"),
            ("nothing of the plan", {"README.md": "changed\n"}, "changes README.md"),
            ("a plan with no index", {PLAN: "# Plan\nNo index.\n"}, "has no valid index"),
            ("a plan with status in it", {PLAN: plan_text([dict(M1, status="half done")])}, "status, history and rulings")):
        said = f.act(t, "submit_to_audit", "main", commit_hash=f.candidate(t, files))
        check(words in said, f"a plan ticket's candidate with {what} is refused: {said}")
    check(f.board.snapshot(t) == before, "and no refused submission changed the ticket")
    files = {PLAN: plan_text([M1, M2]), LOG: "# Decisions\n\n2026-11-22: three milestones.\n"}
    good = f.candidate(t, files)
    # The Director closes on the integration commit: the same plan, landed on main as a commit of its own.
    landed = f.store.commit([f.base], files, f"{t}: integrated")
    f.store.publish("main", landed)
    said = plan_ticket_to_done(f, t, good, close_on=landed)
    check(said == "ok" and f.where(t)[0] == "done", f"the plan ticket goes through Audit, User Review and Final: {said}")
    raw = f.board.sql("SELECT coalesce(ticket_board.approved_plan()::text, '');")
    check(raw, "a closed plan ticket leaves an approved plan")
    approved = json.loads(raw)
    check(approved["commit"] == landed and approved["ticket"] == t and [m["id"] for m in approved["index"]["milestones"]]
          == ["M1", "M2"], f"the index read at the commit it was closed on is the approved plan, and nothing else: {approved}")
    return landed


def deliverables(f: Flow) -> None:
    t = "PGU-920"
    f.seed(t)
    said = f.act(t, "set_ticket_deliverable", "director", deliverable="D9")
    check("D9 is not a deliverable of the approved plan" in said and "D1.1, D1.2, D2.1" in said
          and "request-scope-change" in said, f"an unknown deliverable is refused, naming the plan's: {said}")
    check(f.act(t, "set_ticket_deliverable", "director", deliverable="D1.1") == "ok"
          and "Deliverable: D1.1, of milestone M1 (Ingest)." in last_comment(f.board, t), "a deliverable of the plan")
    check(f.act(t, "route", "director", state="in_progress", assignee="app") == "ok", "routed, serving D1.1")
    later = "PGU-921"
    f.seed(later)
    check(f.act(later, "set_ticket_deliverable", "director", deliverable="D2.1") == "ok", "a deliverable of M2")
    said = f.act(later, "route", "director", state="in_progress", assignee="app")
    check("re-plan due: milestone M2 (Query) is not detailed yet" in said and f.where(later)[0] == "analysis",
          f"routing into an undetailed milestone is refused, re-plan due: {said}")
    spike = "PGU-922"
    f.seed(spike)
    check(f.act(spike, "set_research_brief", "director", question="Which parser?", timebox="2h",
                usable_answer="A name.") == "ok"
          and f.act(spike, "set_ticket_deliverable", "director", deliverable="D2.1") == "ok"
          and f.act(spike, rs.START, "director") == "ok", "a research ticket may name a deliverable, even of M2")


def milestone_notice(f: Flow) -> None:
    def notices() -> str:
        return f.board.sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'enqueue' "
                           "AND detail->'payload'->>'kind' = 'plan_milestone_complete';")

    f.pass_()
    check(f.act("PGU-920", "cancel", "director") == "ok" and notices() == "0",
          "one deliverable's tickets closed is not the milestone: D1.2 has none yet")
    t = "PGU-930"
    f.seed(t)
    check(f.act(t, "set_ticket_deliverable", "director", deliverable="D1.2") == "ok"
          and f.act(t, "route", "director", state="in_progress", assignee="ops") == "ok", "D1.2's ticket")
    check(notices() == "0", "a milestone with open work is not complete")
    check(f.act(t, "cancel", "director") == "ok" and notices() == "2",
          "the last one closed: the Director and the plan owner are told")
    told = f.pass_()
    for who in ("director", "main"):
        said = [m for m in to(told, who) if "Milestone M1" in m]
        check(len(said) == 1 and "re-plan is due" in said[0] and "M2 (Query) is not detailed yet" in said[0],
              f"{who} is told the re-plan is due: {to(told, who)}")
    extra = "PGU-931"
    f.seed(extra)
    check(f.act(extra, "set_ticket_deliverable", "director", deliverable="D1.1") == "ok"
          and f.act(extra, "route", "director", state="in_progress", assignee="app") == "ok"
          and f.act(extra, "cancel", "director") == "ok" and notices() == "2", "and only once")


def scope_changes(f: Flow) -> None:
    t = "PGU-940"
    f.seed(t, "Export to CSV")
    check(f.act(t, "request_scope_change", "main", reason="x") != "ok", "only the Director raises a scope change")
    check("4000 characters" in f.act(t, "request_scope_change", "director", reason=" "), "a scope change says why")
    said = f.act(t, "request_scope_change", "director", reason="The plan has no export deliverable.")
    plans = f.board.sql("SELECT string_agg(id, ',' ORDER BY id) FROM ticket_board.tickets WHERE "
                        "workflow_flags->>'plan_change' = 'true' AND state NOT IN ('done', 'cancelled');")
    check(said == "ok" and len(plans.split(",")) == 1, f"a scope change opens one plan ticket: {said} {plans}")
    plan = plans
    check(f.where(plan) == ["in_progress", "main"] and row(f.board, plan)["flags"].get("plan_change") is True
          and f.board.sql(f"SELECT needs_user_signoff FROM ticket_board.tickets WHERE id = '{plan}';") == "t",
          f"for the plan owner, in Implementation, needing the User's acceptance: {f.where(plan)}")
    blockers = f.board.sql(f"SELECT string_agg(blocker_ticket_id, ',') FROM ticket_board.ticket_blockers WHERE ticket_id = '{t}';")
    check(blockers == plan and "waits on plan ticket" in f.board.sql(
        f"SELECT blocked_reason FROM ticket_board.tickets WHERE id = '{t}';"), "and the ticket waits on it")
    other = "PGU-945"
    f.seed(other)
    check(f.act(other, "request_scope_change", "director", reason="And an import.") == "ok"
          and f.board.sql(f"SELECT string_agg(blocker_ticket_id, ',') FROM ticket_board.ticket_blockers WHERE ticket_id = '{other}';") == plan
          and "Scope change requested by the Director for PGU-945" in last_comment(f.board, plan),
          "a second scope change extends the open plan ticket")
    check("a plan ticket, not" in f.act(plan, "request_scope_change", "director", reason="x"),
          "a plan ticket does not wait on itself")
    spike = "PGU-922"
    check(f.act(spike, rs.SUBMIT_NO_CODE, "researcher", reason="Use the stream parser.") == "ok"
          and f.act(spike, rs.SIGN_OFF, "audit") == "ok"
          and f.act(spike, "request_scope_change", "director", reason="The finding needs a parser deliverable.") == "ok",
          "a research finding may lead to a scope change")


def findings(f: Flow, approved: str) -> None:
    def count(ticket: str, kind: str) -> str:
        return f.board.sql(f"SELECT count(*) FROM ticket_board.plan_findings WHERE ticket_id = '{ticket}' AND kind = '{kind}';")

    t = "PGU-950"
    f.seed(t)
    check(f.act(t, "set_ticket_deliverable", "director", unplanned_reason="A bug.") == "ok"
          and f.act(t, "route", "director", state="in_progress", assignee="app") == "ok", "an unplanned bug fix")
    touched = f.store.commit([approved], {PLAN: plan_text([M1, M2]) + "Rewritten while fixing a bug.\n", "src/b.py": "x\n"},
                             "bug fix that edits the plan")
    f.store.publish("roles/app/pgu-950", touched)
    f.pass_()
    check(f.act(t, "submit_to_audit", "app", commit_hash=touched) == "ok", "the submission itself is not refused")
    check(count(t, "plan_file_touched") == "1" and count(t, "plan_size") == "0"
          and "Plan finding: " in f.board.sql(f"SELECT string_agg(text, '|') FROM ticket_board.ticket_comments WHERE ticket_id = '{t}';")
          and "plan changes go through a plan ticket" in f.board.sql(
              f"SELECT string_agg(text, '|') FROM ticket_board.ticket_comments WHERE ticket_id = '{t}';"),
          "a non-plan candidate that changes the plan is flagged, for Audit on the ticket")
    told = [m for m in to(f.pass_(), "director") if "changes the plan file" in m]
    check(len(told) == 1, f"and to the Director: {told}")
    comments = lambda: f.board.sql(f"SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = '{t}' "  # noqa: E731
                                   "AND text LIKE 'Plan finding:%';")
    check(comments() == "1", "one finding comment")
    check(f.act(t, "audit_kick_back", "audit", reason="plan changes go through a plan ticket") == "ok"
          and f.act(t, "submit_to_audit", "app", commit_hash=touched) == "ok" and count(t, "plan_file_touched") == "1"
          and comments() == "1", "once per commit: no second record, no second comment")
    # The plan ticket the scope changes opened, main's active work.
    big = f.board.sql("SELECT id FROM ticket_board.tickets WHERE workflow_flags->>'plan_change' = 'true' "
                      "AND state = 'in_progress';")
    grown = f.store.commit([approved], {PLAN: plan_text([M1, M2], filler=150)}, "a plan that grew")
    f.store.publish(f"roles/main/{big.lower()}", grown)
    check(f.act(big, "submit_to_audit", "main", commit_hash=grown) == "ok" and count(big, "plan_size") == "1"
          and "over this project's limit of 120" in last_comment(f.board, big),
          "a plan over the size limit is flagged, on a plan ticket too")
    docs = "PGU-952"
    f.seed(docs)
    check(f.act(docs, "set_ticket_deliverable", "director", unplanned_reason="Docs.") == "ok"
          and f.act(docs, "set_workflow_flags", "director", needs_documentation=True) == "ok"
          and f.act(docs, "route", "director", state="in_progress", assignee="ops") == "ok", "a ticket for documentation")
    candidate = f.store.commit([approved], {"src/c.py": "x\n"}, "work")
    f.store.publish("roles/ops/pgu-952", candidate)
    check(f.act(docs, "submit_to_audit", "ops", commit_hash=candidate) == "ok" and f.where(docs)[0] == "documentation",
          "in Documentation")
    edit = f.store.commit([candidate], {PLAN: plan_text([M1, M2]) + "A ruling.\n"}, "docs into the plan")
    ref = candidate_ref("director", docs, candidate)
    f.store.publish(ref, edit)
    said = f.act(docs, "documentation_sign_off", "director", commit_hash=edit, ref=ref)
    check("the plan changes only through a plan ticket" in said and "decision log" in said,
          f"a documentation commit that changes the plan is refused: {said}")
    ruling = f.store.commit([candidate], {LOG: "# Decisions\n\nA ruling found in the work.\n"}, "ruling to the log")
    f.store.publish(ref, ruling)
    check(f.act(docs, "documentation_sign_off", "director", commit_hash=ruling, ref=ref) == "ok",
          "one that moves a ruling to the decision log is not")
    status = json.loads(f.board.sql("SELECT ticket_board.plan_status()::text;"))
    m1 = status["milestones"][0]
    check(status["approved"]["commit"] == approved and m1["replan_due"] is True and m1["deliverables"][0]["id"] == "D1.1"
          and m1["deliverables"][0]["tickets"].get("cancelled") == 2 and status["milestones"][1]["replan_due"] is False
          and status["unplanned"].get("cancelled") == 1, f"plan_status counts by deliverable and state: {status}")


def cli(f: Flow) -> None:
    program = ROOT / "scripts/ticket-board-write"

    def run(caller: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8",
               "TICKET_BOARD_URL": f.board.base, "TICKET_BOARD_CALLER_ROLE": caller,
               "TICKET_BOARD_WRITE_TOKEN": f.board.server.write_token}
        return subprocess.run([sys.executable, str(program), *args], capture_output=True, text=True, env=env, timeout=60)

    count = lambda: f.board.sql("SELECT count(*) FROM ticket_board.tickets;")  # noqa: E731
    before = count()
    done = run("director", "create-ticket", "--title", "x", "--body", "b", "--deliverable", "D1.1", "--unplanned-reason", "y")
    check(done.returncode != 0 and "one of the two" in done.stderr and count() == before,
          f"one of the two, refused before anything is created: {done.stderr}")
    done = run("director", "create-ticket", "--title", "x", "--body", "b", "--plan", "--deliverable", "D1.1")
    check(done.returncode != 0 and "serves no deliverable" in done.stderr and count() == before,
          f"a plan ticket serves none, refused before anything is created: {done.stderr}")
    done = run("director", "create-ticket", "--title", "Reader fix", "--body", "b", "--deliverable", "D1.1")
    check(done.returncode == 0, f"create-ticket --deliverable: {done.stderr[-300:]}")
    made = json.loads(done.stdout)["id"]
    check(f.board.sql(f"SELECT deliverable FROM ticket_board.ticket_deliverables WHERE ticket_id = '{made}';") == "D1.1",
          "it serves D1.1")
    reason = Path(f.store.path.parent) / "reason.txt"
    reason.write_text("A platform report.\n")
    done = run("director", "set-deliverable", made, "--unplanned-reason-file", str(reason))
    check(done.returncode == 0 and f.board.sql(
        f"SELECT unplanned_reason FROM ticket_board.ticket_deliverables WHERE ticket_id = '{made}';") == "A platform report.",
        f"set-deliverable from a file: {done.stderr[-300:]}")
    done = run("director", "create-ticket", "--title", "Plan touch-up", "--body", "b", "--plan")
    plan = json.loads(done.stdout)["id"] if done.returncode == 0 else ""
    check(plan and row(f.board, plan)["flags"].get("plan_change") is True
          and f.board.sql(f"SELECT needs_user_signoff FROM ticket_board.tickets WHERE id = '{plan}';") == "t",
          f"create-ticket --plan: a plan ticket needing the User: {done.stderr[-300:]}")
    check(run("director", "plan-ticket", plan, "--off").returncode == 0
          and row(f.board, plan)["flags"].get("plan_change") is False, "plan-ticket --off")
    done = run("director", "request-scope-change", made, "--reason", "Reports need a deliverable.")
    check(done.returncode == 0 and f.board.sql(
        f"SELECT count(*) FROM ticket_board.ticket_blockers WHERE ticket_id = '{made}';") == "1",
        f"request-scope-change: {done.stderr[-300:]}")


def disable(f: Flow) -> None:
    off = pg.without_plan_gate(f.board.app.workflow_configuration())

    def apply(dry_run: bool) -> str:
        try:
            f.board.app.apply_workflow(off, expected_revision=f.board.app.workflow_document()["revision"],
                                       dry_run=dry_run, caller_role="director")
        except Exception as exc:  # noqa: BLE001 - a refusal is an answer
            return str(exc)
        return "applied"

    said = apply(False)
    check("cannot be turned off while a plan ticket is open" in said, f"disable is refused while planning: {said}")
    for ticket in f.board.sql("SELECT string_agg(id, ' ') FROM ticket_board.tickets WHERE workflow_flags->>'plan_change' = 'true' "
                              "AND state NOT IN ('done', 'cancelled');").split():
        if f.where(ticket)[0] == "audit":  # cancelling a review needs its sign-off: hand it back first
            f.act(ticket, "audit_kick_back", "audit", reason="the plan gate is being turned off")
        check(f.act(ticket, "cancel", "director") == "ok", f"{ticket} cancelled: {f.where(ticket)}")
    check(apply(False) == "applied" and "plan" not in f.board.app.workflow_configuration(), "and accepted once none is")
    check("runs no plan gate" in f.act("PGU-901", "set_ticket_deliverable", "director", unplanned_reason="x"),
          "a board without the gate names no deliverable")


#: The command test's project: a slug no host registers.
SLUG = "rsx843"


def command(cluster) -> None:
    from research_stage_test import CommandBoard
    import research_stage_test

    research_stage_test.SLUG = SLUG
    board = CommandBoard(cluster)
    try:
        launcher_dir = cluster.root / "launcher843"
        launcher_dir.mkdir()
        project_dir = cluster.root / "project843"
        (project_dir / "docs" / "onboarding").mkdir(parents=True)
        (cluster.root / f"{SLUG}.project.json").write_text(json.dumps({"project": {"repository": str(project_dir)}}))
        config_path = launcher_dir / f"{SLUG}.json"
        config_path.write_text(json.dumps({
            "project": SLUG, "layout": "layout.json", "board_url": board.base,
            "roles": [{"role": "main", "cli": ["/nonexistent/claude"], "slot": 3, "model": "the-template-model"}],
            "worktree_base": str(cluster.root / "worktrees843"), "repository": str(project_dir)}))
        os.environ["TICKET_BOARD_WRITE_TOKEN"] = board.server.write_token
        os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
        importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
        importlib.reload(importlib.import_module("scripts.workflow_manage"))
        launcher = importlib.import_module("scripts.team_launcher")
        check("plan-gate" in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS, "the Director turns it on without root")
        previous = launcher.DEFAULT_CONFIG_DIR
        launcher.DEFAULT_CONFIG_DIR = launcher_dir
        try:
            resolved = launcher._resolve_switchyard_project(SLUG).config_path
            assert Path(resolved) == config_path, f"{SLUG} resolves to {resolved}, not this test's configuration"

            def run(*args: str) -> tuple[object, str, str]:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        code = launcher.switchyard_main(["plan-gate", *args])
                    except SystemExit as exc:
                        code = exc.code
                    except Exception as exc:  # a fault in the command is a failed check, not a crash
                        code = f"{type(exc).__name__}: {exc}"
                return code, out.getvalue(), err.getvalue()

            stored = lambda: json.loads(board.sql("SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))  # noqa: E731
            before = stored()
            code, out, _ = run(SLUG, "enable", "--owner", "main", "--dry-run")
            check(code == 0 and '"plan_change"' in out and stored() == before, f"a dry run changes nothing: {code}")
            code, _, err = run(SLUG, "enable")
            check(code != 0 and "one of the two" in err, f"enable needs --owner or --runtime: {err[-200:]}")
            code, _, err = run(SLUG, "enable", "--owner", "main", "--plan-file", "plan/PLAN.md", "--max-lines", "800")
            check(code == 0 and stored()["plan"] == {"owner": "main", "file": "plan/PLAN.md", "decision_log": LOG,
                                                     "max_lines": 800},
                  f"enable --owner, with the plan's settings: {code} {err[-300:]}")
            code, _, err = run("disable", "--project", SLUG)
            check(code == 0 and "plan" not in stored(), f"disable removes it: {code} {err[-300:]}")
            code, _, err = run(SLUG, "enable", "--runtime", "codex")
            entry = next((r for r in json.loads(config_path.read_text())["roles"] if r["role"] == "designer"), None)
            check(code == 0 and stored()["plan"]["owner"] == "designer" and entry is not None
                  and entry.get("cli") == ["codex"], f"enable --runtime declares a Designer: {code} {err[-300:]} {entry}")
        finally:
            launcher.DEFAULT_CONFIG_DIR = previous
            os.environ.pop("TICKET_BOARD_WRITE_TOKEN", None)
            os.environ.pop("TICKET_BOARD_CALLER_ROLE", None)
    finally:
        board.close()


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    with temporary_cluster(prefix="syrd843-", shutdown="immediate") as cluster:
        sql_validator(cluster)
        scenarios(cluster)
        command(cluster)
    print(f"plan_gate_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
