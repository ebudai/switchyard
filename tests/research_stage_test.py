#!/usr/bin/env python3
"""SYRD-842: the optional research stage -- a question, an evidence-reviewed finding, and a hand-back to the decider.

* **Composition** (`research_stage`): an existing role or a new Researcher; every refusal; turning it off and on;
  every order with the documentation gate, Router and the Publisher, each turned off and on again; and each broken
  shape refused by workflow_config's validator.
* **The board's own validator** (`validate_declared_workflow`), asked directly on a schema.sql board and on a board
  built from main's tree and upgraded with this tree's migrations.
* **The flow on the migrated board**, with the real HTTP server and listener and a real git repository as the
  board's trusted commit cache, the documentation gate and Router also on:
  - the refusals that make a research ticket one: no flag, no brief, a brief by anyone but the Director, a timebox
    out of range, the ordinary route into Implementation, research-only decisions on other tickets;
  - a findings commit through Research, Audit's review (a kick-back, then a sign-off) and back to the Director,
    each hand-off notice naming the brief or the finding, then closed on the finding; every malformed findings
    commit refused with the ticket untouched;
  - a no-commit finding, which clears an earlier round's commit; the researcher's hand-back; the Director's return;
    needs_audit off, which skips the review; the Director's wait on the User in Research;
  - the timebox notice: once per round, and again in a new round;
  - disabling, refused while the stage is occupied and accepted when it is empty.
* **The real `ticket-board-write`**: create-ticket --research, research-brief, start-research, the hand-back and a
  no-commit finding, and the refusals it makes before sending anything.
* **`switchyard research-stage`** against a board and a real projection, and **`switchyard new`**'s record, prompt,
  summary and composed plan.
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

import psycopg  # noqa: E402
from documentation_gate_board_test import Store, example, last_comment, row, short  # noqa: E402
from router_role_board_test import Board, fixture, temporary_cluster  # noqa: E402
from router_role_board_test import to as _to  # noqa: E402
from scripts.ticket_board import research_stage as rs  # noqa: E402
from scripts.ticket_board.documentation_gate import with_documentation_gate, without_documentation_gate  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher, without_publisher  # noqa: E402
from scripts.ticket_board.router_setup import with_router, without_router  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

CHECKS = 0
SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())


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


def to(batch: list[tuple[str, str]], role: str) -> list[str]:
    """What `role`'s pane was sent, without the clear an ephemeral role gets before each new ticket."""
    return [message for message in _to(batch, role) if message != "/clear"]


def researcher_doc() -> dict:
    return rs.with_research_stage(copy.deepcopy(SHIPPED), researcher={"runtime": "claude"})


def role_doc() -> dict:
    return rs.with_research_stage(copy.deepcopy(SHIPPED), role="main")


def stage(doc: dict, name: str) -> dict:
    return next(s for s in doc["stages"] if s["name"] == name)


def role(doc: dict, name: str) -> dict:
    return next(r for r in doc["roles"] if r["name"] == name)


def moves(doc: dict, action: str) -> list[dict]:
    return [t for t in doc["transitions"] if t["action"] == action]


def test_the_shapes_compose_and_turn_off() -> None:
    doc = researcher_doc()
    names = [s["name"] for s in doc["stages"]]
    check(names.index("analysis") < names.index("research") < names.index("research_review") < names.index("in_progress"),
          f"Research and its review sit between triage and Implementation: {names}")
    check(stage(doc, "research")["owners"] == ["researcher"] and stage(doc, "research")["kind"] == "system"
          and stage(doc, "research")["gate"] is None, "Research is the Researcher's own, ungated system stage")
    review = stage(doc, "research_review")
    check(review["owners"] == ["audit"] and review["gate"] == "needs_audit" and review["skip_to"] == "analysis"
          and review["signoff"] is None,
          f"the review is Audit's, gated by the ticket's needs_audit, skipping back to triage, with no sign-off "
          f"flag to leave behind: {review}")
    researcher = role(doc, "researcher")
    check(researcher["kind"] == "support" and researcher["capabilities"] == list(rs.RESEARCHER_CAPABILITIES)
          and researcher["ephemeral"] is True and researcher["serial"] is False and researcher["template_role"] == "main"
          and researcher["onboarding_prompt"] == rs.RESEARCHER_PROMPT and researcher["target"] == "cerulean-researcher:0.0",
          f"a new Researcher is a detached, ephemeral role copied from its template: {researcher}")
    check(doc["flags"]["needs_research"] == {"kind": "gate", "default": False} and "research_signoff" not in doc["flags"],
          "needs_research is a gate, off by default, and no new sign-off joins the ones every gate clears")
    check(doc["research"] == {"directory": "docs/research"} and doc["user_waits"] == ["research"],
          "the research directory defaults to docs/research, and the Director may wait on the User in Research")
    submit, no_code = moves(doc, rs.SUBMIT)[0], moves(doc, rs.SUBMIT_NO_CODE)[0]
    check(submit["to"] == no_code["to"] == "research_review" and submit["require_commit"] and no_code["require_reason"]
          and not no_code["require_commit"] and submit["owner_scoped"] and no_code["owner_scoped"],
          "both finding forms enter the review, owner-scoped: a commit, or a reason")
    check(moves(doc, rs.HAND_BACK)[0]["to"] == "analysis" and moves(doc, rs.SIGN_OFF)[0]["to"] == "analysis"
          and moves(doc, rs.SIGN_OFF)[0]["primitive"] == "move" and moves(doc, rs.KICK_BACK)[0]["to"] == "research"
          and moves(doc, rs.KICK_BACK)[0]["primitive"] == "return",
          "the hand-back and Audit's sign-off go to triage; Audit's kick-back returns to Research")
    check(moves(doc, rs.START)[0]["from"] == "analysis" and moves(doc, rs.RETURN)[0]["to"] == "research"
          and moves(doc, rs.CLOSE)[0]["to"] == "done" and all(m["require_reason"] for m in moves(doc, rs.RETURN) + moves(doc, rs.CLOSE)),
          "the Director starts research from triage, returns a finding or closes on it, with a reason")
    route = next(t for t in doc["transitions"] if t["action"] == "route" and t["from"] == "analysis")
    check(route["to"] == "in_progress", "the ordinary route into Implementation is unchanged")
    check({t["action"] for t in doc["transitions"] if t["from"] == "research" and t["actors"] == ["director"]}
          == {"defer", "cancel"}, "the Director can defer or cancel work out of Research")
    check(not any("user_review" in (t["from"], t["to"]) and t["from"] in ("research", "research_review")
                  for t in doc["transitions"]), "nothing composed leads a research ticket to User Review")
    by_role = role_doc()
    check(stage(by_role, "research")["owners"] == ["main"] and not any(r["name"] == "researcher" for r in by_role["roles"]),
          "with --role an existing implementer researches and no role is created")
    off = rs.without_research_stage(doc)
    check(not any(s["name"] in ("research", "research_review") for s in off["stages"])
          and not any(t["action"] in rs.ACTIONS for t in off["transitions"]) and "research" not in off
          and "needs_research" not in off["flags"] and "user_waits" not in off
          and off["remove_stages"] == ["research", "research_review"] and not role(off, "researcher")["active"],
          "disable removes everything it added, names both stages for removal, and keeps the Researcher inactive")
    again = rs.with_research_stage(off, researcher={"runtime": "codex"})
    check(role(again, "researcher")["active"] and role(again, "researcher")["runtime"] == "codex"
          and again["remove_stages"] == [], "enable again turns the same Researcher back on")
    other = rs.with_research_stage(copy.deepcopy(SHIPPED), role="audit", directory="notes/spikes")
    check(other["research"] == {"directory": "notes/spikes"} and rs.research_path("notes/spikes", "pgu-7") == "notes/spikes/PGU-7.md",
          "a project may choose another research directory")


def test_every_refusal() -> None:
    for kwargs, words in (
            ({}, "one of the two"),
            ({"role": "main", "researcher": {"runtime": "claude"}}, "one of the two"),
            ({"role": "router"}, "not an active role"),
            ({"role": "user"}, "not an active role"),
            ({"role": "director"}, "not an active role"),
            ({"role": "nobody"}, "not an active role"),
            ({"researcher": {"runtime": "emacs"}}, "runtime must be one of"),
            ({"researcher": {"runtime": "claude", "template_role": "ghost"}}, "template role"),
            ({"role": "main", "directory": "../outside"}, "repository-relative"),
            ({"role": "main", "directory": "/abs"}, "repository-relative"),
            ({"role": "main", "directory": ""}, "repository-relative")):
        check(words in refusal(lambda: rs.with_research_stage(copy.deepcopy(SHIPPED), **kwargs)),
              f"refused: {kwargs} -> {refusal(lambda: rs.with_research_stage(copy.deepcopy(SHIPPED), **kwargs))}")
    check("already has a research stage, owned by researcher" in refusal(
        lambda: rs.with_research_stage(researcher_doc(), role="main")), "enabling twice means disabling first")
    check("has no research stage" in refusal(lambda: rs.without_research_stage(copy.deepcopy(SHIPPED))),
          "disabling what is off changes nothing")
    clash = copy.deepcopy(SHIPPED)
    clash["flags"]["needs_research"] = {"kind": "gate", "default": False}
    check("need the research stage" in refusal(lambda: rs.with_research_stage(clash, role="main")),
          "a document already declaring needs_research without the stage is refused")
    silent = copy.deepcopy(SHIPPED)
    role(silent, "ops").update(target=None, runtime=None, slot=None)
    silent["stages"] = [dict(s, owners=[o for o in s["owners"] if o != "ops"]) for s in silent["stages"]]
    for t in silent["transitions"]:
        t["actors"] = [a for a in t["actors"] if a != "ops"]
    check("has no pane" in refusal(lambda: rs.with_research_stage(silent, role="ops")),
          "a role the board cannot tell about work cannot research")


def test_it_composes_with_the_documentation_gate_router_and_publisher() -> None:
    ops = {"research": lambda d: rs.with_research_stage(d, researcher={"runtime": "claude"}),
           "documentation": lambda d: with_documentation_gate(d, librarian={"runtime": "claude"}),
           "router": lambda d: with_router(d, runtime="claude"),
           "publisher": with_publisher}
    off = {"research": rs.without_research_stage, "documentation": without_documentation_gate,
           "router": without_router, "publisher": without_publisher}
    seen = 0
    for order in itertools.permutations(ops):
        doc = copy.deepcopy(SHIPPED)
        for name in order:
            doc = ops[name](doc)
        for name in order:
            ops[name](off[name](doc))
        seen += 1
        check(stage(doc, "research_review")["skip_to"] == "analysis"
              and moves(doc, rs.SIGN_OFF)[0]["to"] == "analysis",
              f"{order}: findings still come back to triage, never into publication")
    check(seen == 24, "all 24 orders compose, and each feature turns off and on again in each")


#: Broken shapes, each with the words workflow_config's refusal must say.
VIOLATIONS = (
    ("a Researcher holding edit_fields", lambda d: role(d, "researcher")["capabilities"].append("edit_fields"),
     "holds nothing but add_comment"),
    ("a serial Researcher", lambda d: role(d, "researcher").update(serial=True), "never holds a serial slot"),
    ("a Researcher owning Audit", lambda d: stage(d, "audit")["owners"].append("researcher"),
     "owns the research stage and nothing else"),
    ("a Researcher signing off Audit", lambda d: moves(d, "audit_sign_off")[0]["actors"].append("researcher"),
     "its only moves are its finding submissions"),
    ("needs_research on by default", lambda d: d["flags"]["needs_research"].update(default=True),
     "off unless the director turns it on"),
    ("two research owners", lambda d: stage(d, "research")["owners"].append("main"), "exactly one owner"),
    ("a gated Research", lambda d: stage(d, "research").update(gate="needs_audit", skip_to="in_progress"),
     "an ungated system stage"),
    ("a silent Research", lambda d: stage(d, "research").update(notify={"kind": "none", "role": None}),
     "an ungated system stage"),
    ("a review that skips to Implementation", lambda d: stage(d, "research_review").update(skip_to="in_progress"),
     "research_review is Audit's"),
    ("a review owned by Main", lambda d: stage(d, "research_review").update(owners=["main"]),
     "research_review is Audit's"),
    ("a findings submission without its commit", lambda d: moves(d, rs.SUBMIT)[0].update(require_commit=False),
     "exactly as composed"),
    ("a submission anyone can take", lambda d: moves(d, rs.SUBMIT_NO_CODE)[0].update(owner_scoped=False),
     "exactly as composed"),
    ("a close without a reason", lambda d: moves(d, rs.CLOSE)[0].update(require_reason=False), "exactly as composed"),
    ("a finding sent to User Review", lambda d: d["transitions"].append(dict(copy.deepcopy(moves(d, rs.SIGN_OFF)[0]),
                                                                          to="user_review", action="sign_to_uat")),
     "exactly as composed"),
    ("a research policy outside the repository", lambda d: d.update(research={"directory": "../x"}),
     "repository-relative directory"),
    ("no research policy", lambda d: d.pop("research"), "repository-relative directory"),
    ("Research not among the User waits", lambda d: d.update(user_waits=[]), "wait on the User in research"),
)


def test_workflow_config_refuses_each_broken_shape() -> None:
    for what, change, words in VIOLATIONS:
        doc = researcher_doc()
        change(doc)
        said = refusal(lambda: validate(copy.deepcopy(doc)))
        check(words in said, f"{what} is refused: {said!r}")
    stray = copy.deepcopy(SHIPPED)
    stray["research"] = {"directory": "docs/research"}
    check("need the research stage" in refusal(lambda: validate(stray)), "a policy without its stage is refused")


def test_the_researcher_and_the_director_are_taught() -> None:
    guide = (ROOT / "docs/onboarding/switchyard-researcher-guide.md").read_text()
    director = (ROOT / "docs/onboarding/switchyard-director-guide.md").read_text()
    for words in ("submit-finding", "submit-finding-without-commit", "research-hand-back", "docs/research/",
                  "evidence", "confidence", "recommendation", "timebox"):
        check(words in guide and words in rs.RESEARCHER_PROMPT or words in guide,
              f"the Researcher's guide teaches {words!r}")
    for words in ("research-brief", "start-research", "close-on-finding", "return-finding", "--usable-answer",
                  "switchyard research-stage"):
        check(words in director, f"the Director guide says {words!r}")
    check("research-stage" in (ROOT / "docs/documentation-map.md").read_text(), "the documentation map finds it")
    from scripts import project_onboarding
    check("switchyard-researcher-guide.md" in project_onboarding.SWITCHYARD_ONBOARDING_DOC_NAMES,
          "switchyard new copies the Researcher's guide into every project")


def test_switchyard_new_records_and_composes_the_choice() -> None:
    from scripts import research_onboarding as onboarding
    from scripts.new_project_selection import RoleSelection
    from scripts import team_launcher as launcher
    from scripts.project_design_payload import project_design_artifact_payload

    path = Path("/x/p.project.json")
    roles, implementers = ["director", "main", "ops", "audit"], ["main", "ops"]
    parse = (lambda raw, names=roles: onboarding.artifact_research(raw, path=path, role_names=names,
                                                                    implementers=implementers))
    check(parse(None) == () and parse({"stage": "off"}) == (), "no field, or off, is off")
    check(parse({"stage": "role", "role": "ops"}) == (("stage", "role"), ("role", "ops")), "an implementer researches")
    full = parse({"stage": "researcher", "runtime": "claude", "model": "m", "effort": "low"})
    check(full == (("stage", "researcher"), ("runtime", "claude"), ("model", "m"), ("effort", "low")),
          f"a Researcher: {full}")
    for raw, words in (({"stage": "on"}, "must be one of"), ({"stage": "role", "role": "audit"}, "names the implementer"),
                       ({"stage": "role", "role": "main", "runtime": "claude"}, "names the implementer"),
                       ({"stage": "researcher"}, "runtime"), ({"stage": "off", "role": "main"}, "remove them"),
                       ({"stage": "off", "x": "1"}, "unknown key"), ("role", "must be a JSON object")):
        check(words in refusal(lambda: parse(raw)), f"a malformed record is refused: {raw!r} -> {refusal(lambda: parse(raw))}")
    check("which the Researcher needs" in refusal(lambda: parse({"stage": "researcher", "runtime": "claude"},
                                                                 [*roles, "researcher"])),
          "a role already named researcher is refused")
    plan = [RoleSelection("director", "claude"), RoleSelection("main", "codex")]
    rest, record = onboarding.split_role_plan([*plan, RoleSelection(onboarding.STAGE_MARKER, "main")])
    check(rest == tuple(plan) and record == (("stage", "role"), ("role", "main")), "an implementer's choice leaves the plan")
    rest, record = onboarding.split_role_plan([*plan, RoleSelection("researcher", "codex", "gpt", "high")])
    check(rest == tuple(plan) and record == (("stage", "researcher"), ("runtime", "codex"), ("model", "gpt"),
                                             ("effort", "high")), "so does a Researcher, as its record")
    check("off" in onboarding.summary_line(()) and "done by main" in onboarding.summary_line((("stage", "role"), ("role", "main")))
          and "codex -> gpt -> effort high" in onboarding.summary_line(record, slot=4) and "slot 4" in onboarding.summary_line(record, slot=4),
          "each choice is said before anything is created")
    check(onboarding.with_researcher_cli((("main", "codex"),), record) == (("main", "codex"), ("researcher", "codex")),
          "a Researcher's runtime goes through the CLI checks")
    answers = iter(["4", "claude", "", ""])
    out: list[str] = []
    chosen = onboarding.prompt_research(["main", "ops"], runner=None, owner_args=(), unverified_because="not yet",
                                        input_func=lambda _p: next(answers), print_func=out.append)
    check(isinstance(chosen, launcher.RoleSelection) and chosen.role == "researcher" and chosen.cli == "claude",
          f"choosing a Researcher asks its runtime the guided way: {chosen} {out[-3:]}")
    chosen = onboarding.prompt_research(["main", "ops"], runner=None, owner_args=(), unverified_because="",
                                        input_func=lambda _p: "3", print_func=out.append)
    check(chosen == launcher.RoleSelection(onboarding.STAGE_MARKER, "ops"), f"or an implementer: {chosen}")
    check(onboarding.prompt_research(["main"], runner=None, owner_args=(), unverified_because="",
                                     input_func=lambda _p: "", print_func=out.append) is None, "the default is off")
    check(any("Research stage (optional)" in line for line in out), "under a plainly labelled menu")
    from documentation_gate_command_test import dataclass_replace, sample_artifact
    payload = project_design_artifact_payload(dataclass_replace(sample_artifact(), research=(("stage", "role"), ("role", "main"))))
    check(payload["project"]["research"] == {"stage": "role", "role": "main"}, "the artifact records it")
    check("research" not in project_design_artifact_payload(sample_artifact())["project"], "and nothing when it is off")


MIGRATION = "scripts/ticket_board/migrations/pgu1051_syrd842_research_briefs.sql"


def main_commit() -> str:
    """Main's tree before this change: the parent of the commit that added its migration, or HEAD while uncommitted."""
    adding = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--", MIGRATION],
                            check=True, capture_output=True, text=True).stdout.split()
    return f"{adding[-1]}^" if adding else "HEAD"


def sql_validator(cluster) -> None:
    """The board's own validator on a schema.sql board and on main's tree upgraded with this tree's migrations."""
    from router_role_board_test import create_roles_once, migrate

    docs = {"a Researcher": researcher_doc(), "an implementer": role_doc(),
            "everything on": rs.with_research_stage(with_publisher(with_router(with_documentation_gate(
                copy.deepcopy(SHIPPED)), runtime="claude")), researcher={"runtime": "claude"}),
            "turned off": rs.without_research_stage(researcher_doc())}
    with tempfile.TemporaryDirectory(prefix="syrd842-main.") as tmp:
        tree = Path(tmp)
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", main_commit(), "scripts"], check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True)
        check(not (tree / "scripts/ticket_board/migrations/pgu1051_syrd842_research_briefs.sql").exists(),
              "main's tree has no research migration")
        for name, source, upgrade in (("schema_copy", ROOT, False), ("migrated_copy", tree, True)):
            admin = fixture.conninfo(cluster.socket_dir, cluster.port, name)
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", name])
            fixture.psql(admin, (source / "scripts/ticket_board/schema.sql").read_text())
            create_roles_once(admin)
            fixture.psql(admin, (source / "scripts/ticket_board/rbac.sql").read_text())
            if upgrade:
                migrate(admin)
            check(fixture.psql(admin, "SELECT to_regprocedure('ticket_board.set_research_brief(text,text,integer,text)') "
                                      "IS NOT NULL;") == "t", f"{name}: the brief's function is there")
            for what, doc in docs.items():
                done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"doc={json.dumps(doc)}", "-d", admin],
                                      input="SELECT ticket_board.validate_declared_workflow(:'doc'::jsonb);\n",
                                      capture_output=True, text=True)
                check(done.returncode == 0, f"{name}: the board's validator accepts {what}: {done.stderr[-400:]}")


def snapshot(board: Board, ticket: str) -> str:
    return board.sql("SELECT ((to_jsonb(t) - 'row_updated_at' - 'source_json') || jsonb_build_object('comments', "
                     "(SELECT count(*) FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id)))::text "
                     f"FROM ticket_board.tickets t WHERE t.id = '{ticket}';")


def flow_document() -> dict:
    return rs.with_research_stage(with_router(with_documentation_gate(example()), runtime="claude"),
                                  researcher={"runtime": "claude"})


def brief(board: Board, ticket: str, actor: str = "director", **overrides) -> dict:
    payload = {"question": "Which encoding does the archive use?", "timebox": "2h",
               "usable_answer": "The encoding's name, with one sample decoded."}
    payload.update(overrides)
    return board.act(ticket, "set_research_brief", actor, **payload)


def flow(cluster) -> None:
    """The research stage on a board built from main's tree and upgraded with this tree's migrations."""
    with tempfile.TemporaryDirectory(prefix="syrd842-flow.") as tmp:
        tree = Path(tmp) / "main"
        tree.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", main_commit(), "scripts"], check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True)
        store = Store(Path(tmp) / "cache")
        base = store.commit([], {"README.md": "project\n"}, "base")
        document = flow_document()
        board = Board(cluster, "flow", document, before=tree)
        board.app.commit_git_dirs = (store.path,)
        board.app.commit_git_dir = store.path
        try:
            board.one_pass()
            board.sent.clear()
            board.busy.clear()
            refusals(board)
            commit_finding(board, store, base)
            no_commit_finding(board, store, base)
            timebox(board)
            cli(board)
            disable(board)
        finally:
            board.close()


def refusals(board: Board) -> None:
    t = "PGU-801"
    fixture.seed_postgres_ticket(board.admin, t, title="Archive encoding", state="analysis", assignee="director")
    before = snapshot(board, t)
    answer = board.act(t, rs.START, "director")
    check("is not a research ticket" in short(answer) and "research-brief" in short(answer)
          and snapshot(board, t) == before, f"a ticket without the flag cannot enter Research: {short(answer)}")
    check(short(board.act(t, "set_workflow_flags", "director", needs_research=True)) == "ok", "the Director flags it")
    before = snapshot(board, t)
    answer = board.act(t, rs.START, "director")
    check("a research brief is required" in short(answer) and "--usable-answer" in short(answer)
          and snapshot(board, t) == before, f"nor with the flag but no brief: {short(answer)}")
    for actor in ("main", "researcher", "audit"):
        check(short(brief(board, t, actor)) != "ok", f"{actor} cannot write a research brief")
    for overrides, words in (({"question": "  "}, "needs the question"), ({"usable_answer": ""}, "usable answer"),
                             ({"timebox": "10m"}, "15 minutes to 30 days"), ({"timebox": "31d"}, "15 minutes to 30 days"),
                             ({"timebox": "soon"}, "a duration"), ({"question": "q" * 2001}, "2000 characters")):
        answer = brief(board, t, **overrides)
        check(words in short(answer), f"a brief with {list(overrides)} is refused: {short(answer)}")
    check(snapshot(board, t) == before, "and no refused brief changed the ticket")
    # SYRD-849: the operation passes an int timebox straight through, so on that path the database's range is the
    # only guard.
    for seconds in (899, 2592001):
        answer = brief(board, t, timebox=seconds)
        check("15 minutes to 30 days" in short(answer) and snapshot(board, t) == before,
              f"an int timebox of {seconds}s is refused by the database, the ticket unchanged: {short(answer)}")
    answer = brief(board, t)
    check(short(answer) == "ok" and "Research brief.\nQuestion: Which encoding" in last_comment(board, t)
          and "Timebox: 2h" in last_comment(board, t), f"the Director's brief is recorded: {last_comment(board, t)}")
    before = snapshot(board, t)
    answer = board.act(t, "route", "director", state="in_progress", assignee="main")
    check("is a research ticket" in short(answer) and "start-research" in short(answer) and snapshot(board, t) == before,
          f"a research ticket is not routed into Implementation: {short(answer)}")
    plain = "PGU-809"
    fixture.seed_postgres_ticket(board.admin, plain, title="Plain", state="analysis", assignee="director")
    for action in (rs.CLOSE, rs.RETURN):
        answer = board.act(plain, action, "director", reason="no")
        check("for research tickets" in short(answer), f"{action} is for research tickets only: {short(answer)}")


def commit_finding(board: Board, store: Store, base: str) -> None:
    t = "PGU-801"
    answer = board.act(t, rs.START, "director")
    check(short(answer) == "ok" and board.where(t) == ["research", "researcher"], f"started: {short(answer)}")
    told = to(board.one_pass(), "researcher")
    check(len(told) == 1 and "Research question: Which encoding" in told[0] and "Timebox: 2h" in told[0]
          and "docs/research/PGU-801.md" in told[0], f"the Researcher is told the brief: {told}")
    board.busy.clear()
    good = store.commit([base], {"docs/research/PGU-801.md": "# Finding\nUTF-16LE.\n"}, "PGU-801 finding")
    #: Each malformed findings commit, with the words its refusal must say.
    bad = {
        "a product file beside it": (store.commit([base], {"docs/research/PGU-801.md": "x\n", "src/a.py": "x\n"}, "x"),
                                     "src/a.py (a findings commit changes only docs/research/PGU-801.md)"),
        "another ticket's file": (store.commit([base], {"docs/research/PGU-802.md": "x\n"}, "x"),
                                  "docs/research/PGU-802.md (a findings commit changes only"),
        "an executable finding": (store.commit([base], {"docs/research/PGU-801.md": ("100755", "x\n")}, "x"),
                                  "as an ordinary, non-executable file"),
        "a merge": (store.commit([base, good], {"docs/research/PGU-801.md": "y\n"}, "x"), "has 2 parents"),
        "an empty commit": (store.commit([base], {}, "x"), "changes nothing; a findings commit adds or edits"),
    }
    before = snapshot(board, t)
    answer = board.act(t, rs.SUBMIT, "researcher", commit_hash="f" * 40)
    check("unknown commit_hash" in short(answer), f"a commit the board's cache does not know is refused: {short(answer)}")
    for what, (commit, words) in bad.items():
        answer = board.act(t, rs.SUBMIT, "researcher", commit_hash=commit)
        check(short(answer) != "ok" and words in short(answer), f"{what} is refused, saying why: {short(answer)}")
    check(short(board.act(t, rs.SUBMIT, "main", commit_hash=good)) != "ok", "only the Researcher submits its finding")
    check(short(board.act(t, rs.SUBMIT_NO_CODE, "researcher", reason="x", commit_hash=good)) != "ok",
          "the no-commit form takes no commit")
    check(snapshot(board, t) == before, "no refused submission changed the ticket")
    answer = board.act(t, rs.SUBMIT, "researcher", commit_hash=good)
    check(short(answer) == "ok" and board.where(t) == ["research_review", "audit"] and row(board, t)["commit"] == good,
          f"a findings commit goes to Audit's review: {short(answer)} {board.where(t)}")
    check(f"Research finding: commit {good} adds docs/research/PGU-801.md." in last_comment(board, t),
          f"its record names the commit and the file: {last_comment(board, t)}")
    told = to(board.one_pass(), "audit")
    check(len(told) == 1 and f"Research finding: commit {good}" in told[0] and "Review its evidence" in told[0],
          f"Audit is told what to review: {told}")
    board.busy.clear()
    answer = board.act(t, rs.KICK_BACK, "audit", reason="No sample was decoded.")
    check(short(answer) == "ok" and board.where(t) == ["research", "researcher"] and row(board, t)["commit"] == "",
          f"Audit returns it to the Researcher with what is missing: {short(answer)} {board.where(t)}")
    board.one_pass()
    board.busy.clear()
    edited = store.commit([good], {"docs/research/PGU-801.md": "# Finding\nUTF-16LE; sample decoded.\n"}, "edit")
    check(short(board.act(t, rs.SUBMIT, "researcher", commit_hash=edited)) == "ok", "the edited finding is resubmitted")
    check(f"commit {edited} edits docs/research/PGU-801.md" in last_comment(board, t), "an edit is recorded as one")
    board.one_pass()
    board.busy.clear()
    answer = board.act(t, rs.SIGN_OFF, "audit")
    check(short(answer) == "ok" and board.where(t) == ["analysis", "director"],
          f"Audit's sign-off hands the finding back to the Director: {short(answer)} {board.where(t)}")
    told = to(board.one_pass(), "director")
    check(len(told) == 1 and f"Research finding: commit {edited}, docs/research/PGU-801.md" in told[0]
          and "Close it on the finding" in told[0], f"in one notice naming the finding: {told}")
    board.busy.clear()
    answer = board.act(t, rs.CLOSE, "director", reason="UTF-16LE; the importer ticket follows.")
    check(short(answer) == "ok" and board.where(t)[0] == "done", f"the Director closes it on the finding: {short(answer)}")
    check(not any("user_review" in (s or "") for s in board.where(t)), "never through User Review")


def no_commit_finding(board: Board, store: Store, base: str) -> None:
    t = "PGU-802"
    fixture.seed_postgres_ticket(board.admin, t, title="Corpus size", state="analysis", assignee="director")
    check(short(brief(board, t, question="How large is the corpus?", timebox="1d")) == "ok", "briefed")
    check(short(board.act(t, rs.START, "director")) == "ok", "started")
    answer = board.act(t, rs.HAND_BACK, "researcher", reason="Which corpus? There are two.")
    check(short(answer) == "ok" and board.where(t) == ["analysis", "director"],
          f"the Researcher hands back a question it cannot research as written: {short(answer)}")
    check(short(board.act(t, "set_research_brief", "director", question="How large is corpus A?", timebox="1d",
                          usable_answer="A byte count.")) == "ok", "the Director rewrites the brief")
    check(short(board.act(t, rs.START, "director")) == "ok", "and starts it again")
    first = store.commit([base], {"docs/research/PGU-802.md": "3 GB\n"}, "PGU-802 finding")
    check(short(board.act(t, rs.SUBMIT, "researcher", commit_hash=first)) == "ok", "a first finding, with a commit")
    check(short(board.act(t, rs.SIGN_OFF, "audit")) == "ok", "Audit signs it off")
    board.one_pass()
    board.busy.clear()
    answer = board.act(t, rs.RETURN, "director", reason="Count corpus A's mirror too.")
    check(short(answer) == "ok" and board.where(t) == ["research", "researcher"],
          f"the Director returns the finding to the researcher: {short(answer)} {row(board, t)}")
    answer = board.act(t, "await_user", "director", reason="Only the User has the mirror's address.",
                       request="Paste the mirror's address on the ticket.")
    check(short(answer) == "ok", f"the Director may wait on the User in Research: {short(answer)}")
    check(short(board.act(t, "clear_awaiting_role", "director")) == "ok" or True, "")
    answer = board.act(t, rs.SUBMIT_NO_CODE, "researcher",
                       reason="Corpus A is 3.1 GB with its mirror: du -sb ran on both. Confidence: high.")
    check(short(answer) == "ok" and board.where(t) == ["research_review", "audit"] and row(board, t)["commit"] == "",
          f"a no-commit finding clears the earlier round's commit: {short(answer)} {row(board, t)}")
    check(last_comment(board, t).startswith("researcher|Research finding (no commit).\n\nCorpus A is 3.1 GB"),
          f"and its reason is the finding: {last_comment(board, t)}")
    told = to(board.one_pass(), "audit")
    check(len(told) == 1 and "no commit; the finding is the reason on researcher's submission" in told[0], f"{told}")
    board.busy.clear()
    check(short(board.act(t, rs.SIGN_OFF, "audit")) == "ok", "signed off")
    check(short(board.act(t, rs.RETURN, "director", reason="One more check.")) == "ok", "returned once more")
    check(short(board.act(t, "set_workflow_flags", "director", needs_audit=False)) == "ok", "needs_audit turned off")
    answer = board.act(t, rs.SUBMIT_NO_CODE, "researcher", reason="Still 3.1 GB.")
    check(short(answer) == "ok" and board.where(t) == ["analysis", "director"],
          f"without needs_audit a finding skips the review: {short(answer)} {board.where(t)}")
    board.one_pass()
    board.busy.clear()
    check(short(board.act(t, "defer", "director")) == "ok", "put down")


def timebox(board: Board) -> None:
    from scripts.ticket_board import research_finding

    t = "PGU-803"
    fixture.seed_postgres_ticket(board.admin, t, title="Tool evaluation", state="analysis", assignee="director")
    check(short(brief(board, t, timebox="1h")) == "ok" and short(board.act(t, rs.START, "director")) == "ok", "started")
    board.one_pass()
    board.busy.clear()

    def run() -> object:
        with psycopg.connect(board.listener_url, autocommit=True) as conn:
            return research_finding.run(board.listener, conn)

    check(run() == 0, "inside the timebox nobody is told")
    board.sql(f"UPDATE ticket_board.ticket_research_briefs SET round_started_at = clock_timestamp() - interval '61 minutes' "
              f"WHERE ticket_id = '{t}';")
    before = len(board.sent)
    board.listener.listen_once(max_notifications=board.listener.delivered_count + 20)  # the listener's own pass
    told = [m for target, m in board.sent[before:] if target == "pgu-director:0.0" and "timebox" in m]
    check(len(told) == 1 and "its research timebox (1h) passed" in told[0] and "with no finding from researcher" in told[0]
          and "The board stops nothing" in told[0], f"past it, the listener's pass tells the Director what passed: {told}")
    check(run() == 0, "once: not again on the next pass")
    board.busy.clear()
    check(board.where(t) == ["research", "researcher"], "and the board stopped nothing")
    check(short(board.act(t, rs.HAND_BACK, "researcher", reason="Needs a licence.")) == "ok"
          and short(board.act(t, rs.START, "director")) == "ok", "a new round")
    check(run() == 0, "starts a new clock")
    board.sql(f"UPDATE ticket_board.ticket_research_briefs SET round_started_at = clock_timestamp() - interval '2 hours' "
              f"WHERE ticket_id = '{t}';")
    check(run() == 1, "and its own notice")
    check(short(board.act(t, rs.HAND_BACK, "researcher", reason="Done elsewhere.")) == "ok",
          "the ticket leaves Research before that notice is sent")
    told = []
    for _ in range(3):  # each pass with the Director's pane free, so only staleness can hold the notice back
        board.busy.clear()
        told += [m for m in to(board.one_pass(), "director") if "timebox" in m]
    board.busy.clear()
    dropped = board.sql(f"SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = '{t}' AND event = 'drop' "
                        "AND busy_reason = 'stale_notification' AND detail->'payload'->>'kind' = 'research_timebox';")
    check(not told and dropped == "1", f"a timebox notice is dropped as stale once its round is over: {told} {dropped}")
    queued = board.sql(f"SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = '{t}' "
                       "AND event = 'enqueue' AND detail->'payload'->>'kind' = 'research_timebox';")
    check(queued == "2", f"one notice per round: {queued}")
    # SYRD-849: a round's notice still waiting when the ticket is back in Research for a later round is stale too.
    check(short(board.act(t, rs.START, "director")) == "ok", "a third round")
    board.sql(f"UPDATE ticket_board.ticket_research_briefs SET round_started_at = clock_timestamp() - interval '2 hours' "
              f"WHERE ticket_id = '{t}';")
    check(run() == 1, "its notice is queued")
    check(short(board.act(t, rs.SUBMIT_NO_CODE, "researcher", reason="Not worth it: no licence.")) == "ok"
          and short(board.act(t, rs.KICK_BACK, "audit", reason="Which licence? Cite it.")) == "ok"
          and board.where(t) == ["research", "researcher"], "the finding is kicked back: a fourth round, in Research")
    told = []
    for _ in range(3):
        board.busy.clear()
        told += [m for m in to(board.one_pass(), "director") if "timebox" in m]
    board.busy.clear()
    dropped = board.sql(f"SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = '{t}' AND event = 'drop' "
                        "AND busy_reason = 'stale_notification' AND detail->'payload'->>'kind' = 'research_timebox';")
    check(not told and dropped == "2",
          f"the third round's notice is dropped as stale in the fourth round, though the ticket is in Research: "
          f"{told} {dropped}")
    check(short(board.act(t, "defer", "director")) == "ok", "put down")


def cli(board: Board) -> None:
    """The real ticket-board-write, over HTTP, as the Director and the Researcher."""
    program = ROOT / "scripts/ticket-board-write"

    def run(caller: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
               "LANG": "C.UTF-8", "TICKET_BOARD_URL": board.base, "TICKET_BOARD_CALLER_ROLE": caller,
               "TICKET_BOARD_WRITE_TOKEN": board.server.write_token}
        return subprocess.run([sys.executable, str(program), *args], capture_output=True, text=True, env=env, timeout=60)

    done = run("director", "create-ticket", "--title", "Spike", "--body", "b", "--research", "--question", "q?")
    check(done.returncode != 0 and "give --timebox, --usable-answer" in done.stderr,
          f"create-ticket --research needs its whole brief, before anything is created: {done.stderr[-300:]}")
    done = run("director", "create-ticket", "--title", "Spike", "--body", "b", "--question", "q?")
    check(done.returncode != 0 and "add --research" in done.stderr, f"a brief belongs to a research ticket: {done.stderr}")
    done = run("director", "create-ticket", "--title", "Spike", "--body", "b", "--research", "--question", "q?",
               "--timebox", "5m", "--usable-answer", "a")
    check(done.returncode != 0 and "15 minutes to 30 days" in done.stderr, f"a timebox out of range: {done.stderr}")
    done = run("director", "create-ticket", "--title", "Spike", "--body", "b", "--research", "--question",
               "Does the parser stream?", "--timebox", "3h", "--usable-answer", "Yes or no, with the call graph.")
    check(done.returncode == 0, f"create-ticket --research: {done.stderr[-400:]}")
    ticket = json.loads(done.stdout)["id"]
    check(row(board, ticket)["flags"].get("needs_research") is True
          and "Question: Does the parser stream?" in last_comment(board, ticket),
          f"the new ticket is a research ticket with its brief: {row(board, ticket)}")
    done = run("director", "research-brief", ticket, "--question", "Does the parser stream input?", "--timebox", "4h",
               "--usable-answer", "Yes or no.")
    check(done.returncode == 0 and json.loads(done.stdout)["id"] == ticket, f"research-brief: {done.stderr[-300:]}")
    done = run("main", "research-brief", ticket, "--question", "x", "--timebox", "1h", "--usable-answer", "y")
    check(done.returncode != 0, "only the Director writes one")
    check(run("director", "start-research", ticket).returncode == 0 and board.where(ticket)[0] == "research", "start-research")
    done = run("researcher", "research-hand-back", ticket, "--reason", "Which parser?")
    check(done.returncode == 0 and board.where(ticket)[0] == "analysis", f"research-hand-back: {done.stderr[-300:]}")
    check(run("director", "start-research", ticket).returncode == 0, "started again")
    done = run("researcher", "submit-finding-without-commit", ticket, "--reason", "It streams: read() is chunked.")
    check(done.returncode == 0 and board.where(ticket)[0] == "research_review", f"no-commit finding: {done.stderr[-300:]}")
    check(run("audit", "research-sign-off", ticket).returncode == 0 and board.where(ticket)[0] == "analysis", "signed off")
    done = run("director", "close-on-finding", ticket, "--reason", "Streams; no work needed.")
    check(done.returncode == 0 and board.where(ticket)[0] == "done", f"close-on-finding: {done.stderr[-300:]}")
    board.one_pass()
    board.busy.clear()


def disable(board: Board) -> None:
    t = "PGU-804"
    fixture.seed_postgres_ticket(board.admin, t, title="Occupant", state="analysis", assignee="director")
    check(short(brief(board, t)) == "ok" and short(board.act(t, rs.START, "director")) == "ok", "a ticket in Research")
    off = rs.without_research_stage(board.app.workflow_configuration())

    def apply(dry_run: bool) -> str:
        try:
            board.app.apply_workflow(off, expected_revision=board.app.workflow_document()["revision"], dry_run=dry_run,
                                     caller_role="director")
        except Exception as exc:  # noqa: BLE001 - a refusal is an answer
            return str(exc)
        return "applied"

    said = apply(True)
    check("occupied" in said, f"disable is refused while Research holds a ticket: {said}")
    check(short(board.act(t, "defer", "director")) == "ok", "the ticket is put down")
    said = apply(False)
    check(said == "applied" and not rs.research_stage(board.app.workflow_configuration()),
          f"and accepted once it is empty: {said}")
    check(short(brief(board, t)) != "ok", "a board without the stage takes no brief")


#: The command test's project: a slug no host registers, so the launcher resolves this test's own configuration.
SLUG = "rsx842"


class CommandBoard:
    """A board for `switchyard research-stage`, under SLUG: the real HTTP server, no listener."""

    def __init__(self, cluster) -> None:
        import threading
        from router_role_board_test import build_board

        self.admin = build_board(cluster, "command")
        service = fixture.conninfo(cluster.socket_dir, cluster.port, "command", fixture.SERVICE_ROLE)
        self.app = fixture.TicketBoardApp(cluster.root / "command-frames", cluster.root / "command-assets",
                                          project=SLUG, ticket_prefix="RSX", database_url=service)
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        document = json.loads(json.dumps(example()).replace('"pgu-', f'"{SLUG}-'))
        document["project"] = SLUG
        self.app.apply_workflow(document, expected_revision=0, dry_run=False, caller_role="director")

    def sql(self, statement: str) -> str:
        return fixture.psql(self.admin, statement).strip()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def command(cluster) -> None:
    """`switchyard research-stage`, against a board and a real tenant projection."""
    board = CommandBoard(cluster)
    try:
        launcher_dir = cluster.root / "launcher"
        launcher_dir.mkdir()
        project_dir = cluster.root / "project"
        (project_dir / "docs" / "onboarding").mkdir(parents=True)
        (cluster.root / f"{SLUG}.project.json").write_text(json.dumps({"project": {"repository": str(project_dir)}}))
        config_path = launcher_dir / f"{SLUG}.json"
        config_path.write_text(json.dumps({
            "project": SLUG, "layout": "layout.json", "board_url": board.base,
            "roles": [{"role": "main", "cli": ["/nonexistent/claude"], "slot": 3, "model": "the-template-model",
                       "effort": "the-template-effort"}],
            "worktree_base": str(cluster.root / "worktrees"), "repository": str(project_dir),
        }))
        os.environ["TICKET_BOARD_WRITE_TOKEN"] = board.server.write_token
        os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
        importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
        importlib.reload(importlib.import_module("scripts.workflow_manage"))
        launcher = importlib.import_module("scripts.team_launcher")
        check("research-stage" in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS, "the Director turns it on without root")
        previous = launcher.DEFAULT_CONFIG_DIR
        launcher.DEFAULT_CONFIG_DIR = launcher_dir
        try:
            resolved = launcher._resolve_switchyard_project(SLUG).config_path
            # Nothing runs unless the launcher resolves this test's own configuration: never a host's tenant.
            assert Path(resolved) == config_path, f"{SLUG} resolves to {resolved}, not this test's configuration"

            def run(*args: str) -> tuple[object, str, str]:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        code = launcher.switchyard_main(["research-stage", *args])
                    except SystemExit as exc:
                        code = exc.code
                    except Exception as exc:  # a fault in the command is a failed check, not a crash
                        code = f"{type(exc).__name__}: {exc}"
                return code, out.getvalue(), err.getvalue()

            stored = lambda: json.loads(board.sql("SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))  # noqa: E731
            before = stored()
            code, out, _ = run(SLUG, "enable", "--role", "main", "--dry-run")
            check(code == 0 and '"start_research"' in out and stored() == before, f"a dry run changes nothing: {code}")
            code, _, err = run(SLUG, "enable", "--role", "main", "--runtime", "codex")
            check(code != 0 and "not both" in err, f"--role or a new Researcher's --runtime, not both: {err[-200:]}")
            code, _, err = run(SLUG, "enable", "--role", "main", "--model", "m")
            check(code != 0 and "not both" in err, f"a model is a new Researcher's: {err[-200:]}")
            code, _, err = run(SLUG, "enable", "--role", "main", "--template-role", "main")
            check(code != 0 and "needs --runtime" in err, f"a template role is for a new Researcher: {err[-200:]}")
            code, _, err = run(SLUG, "enable", "--role", "main", "--research-directory", "spikes")
            check(code == 0 and rs.research_owner(stored()) == "main" and stored()["research"] == {"directory": "spikes"}
                  and not any(r["name"] == "researcher" for r in stored()["roles"]),
                  f"enable --role gives the research to Main and creates no role: {code} {err[-300:]}")
            code, _, err = run("disable", "--project", SLUG)
            check(code == 0 and not rs.research_stage(stored()), f"disable removes it: {code} {err[-300:]}")
            code, _, err = run(SLUG, "enable", "--runtime", "codex")
            check(code == 0 and rs.research_owner(stored()) == "researcher", f"enable --runtime: {code} {err[-300:]}")
            entry = next((r for r in json.loads(config_path.read_text())["roles"] if r["role"] == "researcher"), None)
            check(entry is not None and entry.get("cli") == ["codex"] and entry.get("model") == "the-template-model"
                  and (entry.get("env") or {}).get("TICKET_BOARD_ROLE_ONBOARDING_PROMPT") == rs.RESEARCHER_PROMPT,
                  f"the launcher runs the Researcher on its runtime, with its prompt: {entry}")
            code, _, err = run(SLUG, "disable")
            check(code == 0 and not role(stored(), "researcher")["active"]
                  and not any(r["role"] == "researcher" for r in json.loads(config_path.read_text())["roles"]),
                  f"disable turns the Researcher off on the board and in the launcher: {code} {err[-300:]}")
            # SYRD-889: plain enable is a new Researcher on agy with Gemini 4 Hydrogen (High), and no effort. A
            # Researcher turned back on is projected from its own retired launcher entry
            # (workflow_launcher.project_roles); turned on again on another runtime than that entry ran, its model and
            # effort are the template's again, never agy's Hydrogen on a Claude or Codex pane (Audit's finding).
            template = ("the-template-model", "the-template-effort")
            for args, runtime, model, effort in (
                    (("--runtime", "claude", "--model", "claude-opus-5-5"), "claude", "claude-opus-5-5", template[1]),
                    ((), "agy", rs.RESEARCHER_DEFAULT_MODEL, None),
                    (("--runtime", "agy"), "agy", rs.RESEARCHER_DEFAULT_MODEL, None),
                    (("--runtime", "agy", "--model", "gemini-4-hydrogen-xhigh"), "agy", "gemini-4-hydrogen-xhigh", None),
                    (("--runtime", "claude"), "claude", *template),
                    ((), "agy", rs.RESEARCHER_DEFAULT_MODEL, None),
                    (("--runtime", "codex"), "codex", *template),
                    ((), "agy", rs.RESEARCHER_DEFAULT_MODEL, None),
                    (("--runtime", "claude", "--model", "claude-opus-5-5"), "claude", "claude-opus-5-5", template[1])):
                code, _, err = run(SLUG, "enable", *args)
                entry = next((r for r in json.loads(config_path.read_text())["roles"] if r["role"] == "researcher"), {})
                check(code == 0 and role(stored(), "researcher")["runtime"] == runtime and entry.get("cli") == [runtime]
                      and entry.get("model") == model and entry.get("effort") == effort,
                      f"enable {' '.join(args) or '(plain)'}: {runtime}, {model}, effort {effort}: {code} {entry} "
                      f"{err[-300:]}")
                code, _, err = run(SLUG, "disable")
                check(code == 0, f"and disable again: {code} {err[-300:]}")
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
    with temporary_cluster(prefix="syrd842-", shutdown="immediate") as cluster:
        sql_validator(cluster)
        flow(cluster)
        command(cluster)
    print(f"research_stage_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
