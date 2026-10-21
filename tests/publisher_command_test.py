#!/usr/bin/env python3
"""SYRD-625: what the Publisher may be, how it is turned on, and that it is never a process.

* Composition: `with_publisher` places the publication stage after Audit, with
  exactly the Publisher's three moves and the Director's two answers to a
  failure; `without_publisher` puts Audit's approvals back; turning it on again
  reuses the same role. Shapes it cannot compose are refused, changing nothing.
* Every rule of the Publisher's shape is enforced by workflow_config: no model,
  runtime, pane, slot, session, prompt, template or capability; one stage; three
  moves; nothing else reaching what publication hands on to.
* The Publisher is never given an account, a pane or a launcher entry: it is one
  of the roles provisioning treats as never a process, and the launcher's
  projection of a document with it starts nothing for it.
* `switchyard publisher enable|disable`, against a board and a real tenant
  projection, through workflow_manage's apply path; the board's own
  validate_declared_workflow accepts both documents.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import ticket_board_write_api_test as t  # noqa: E402
from documentation_gate_command_test import Board  # noqa: E402
from scripts.ticket_board import publisher_stage as ps  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher, without_publisher  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def role(doc: dict, name: str) -> dict:
    return next(r for r in doc["roles"] if r["name"] == name)


def stage(doc: dict, name: str) -> dict:
    return next(s for s in doc["stages"] if s["name"] == name)


def moves(doc: dict) -> dict:
    return {(tr["from"], tr["action"]): tr for tr in doc["transitions"]}


def refusal(fn):
    """The ValueError's text, or the value when there was none: "accepted" when that value is None."""
    try:
        value = fn()
    except ValueError as exc:
        return str(exc)
    return "accepted" if value is None else (value if isinstance(value, dict) else "accepted")


def composed(source: dict = SHIPPED) -> dict:
    """`with_publisher(source)`, where a refusal is a failed check rather than a crash."""
    try:
        return with_publisher(source)
    except ValueError as exc:
        check(False, f"the workflow composes with the Publisher: {exc}")
        raise


def broken(change) -> str:
    """What the validator says about the composed document with one thing changed."""
    doc = composed()
    change(doc)
    return refusal(lambda: validate(doc))


def test_the_stage_composes_validates_and_turns_off() -> None:
    doc = composed()
    check(role(doc, "publisher") == {"name": "publisher", "label": "Publisher", "kind": "system", "active": True,
                                     "capabilities": []},
          f"the Publisher is a system actor with nothing but a name: {role(doc, 'publisher')}")
    names = [s["name"] for s in doc["stages"]]
    check(names.index("publication") == names.index("audit") + 1 and names.index("publication_failed")
          == names.index("publication") + 1, f"the publication stages follow Audit: {names}")
    check(stage(doc, "audit")["skip_to"] == "publication" and moves(doc)[("audit", "audit_sign_off")]["to"]
          == "publication", "Audit's approval and its gate skip both enter publication")
    check(stage(doc, "publication") == {"name": "publication", "label": "Publication", "owners": ["publisher"],
                                        "kind": "system", "gate": None, "skip_to": None, "signoff": None,
                                        "terminal": False, "notify": {"kind": "none", "role": None}},
          "publication is the Publisher's, ungated, signed off by nobody, silent")
    check(stage(doc, "publication_failed")["owners"] == ["director"]
          and stage(doc, "publication_failed")["notify"]["kind"] == "assignee", "a failure is the Director's, told")
    every = sorted(n for n, f in doc["flags"].items() if f["kind"] == "signoff")
    m = moves(doc)
    check(m[("publication", "publication_published")]["to"] == "dat"
          and m[("publication", "publication_published")]["require_commit"]
          and m[("publication", "publication_published")]["primitive"] == "move",
          "a publication hands on to where Audit approved into before, carrying the commit")
    check(m[("publication", "publication_return")]["to"] == "in_progress"
          and m[("publication", "publication_return")]["primitive"] == "return"
          and m[("publication", "publication_return")]["clear_signoffs"] == every
          and m[("publication_failed", "publication_kick_back")]["clear_signoffs"] == every,
          "both returns go to Implementation and clear every sign-off, so every gate is renewed")
    check(all(m[("publication", a)]["actors"] == ["publisher"] and m[("publication", a)]["owner_scoped"]
              and m[("publication", a)]["require_reason"] for a in ps.PUBLISHER_ACTIONS),
          "the Publisher's three moves are its own, owner-scoped, each with its reason")
    check(m[("publication_failed", "publication_retry")]["actors"] == ["director"]
          and m[("publication_failed", "publication_retry")]["to"] == "publication", "the Director can retry")
    check({(k[0], tr["to"]) for k, tr in m.items() if tr["action"] in ("defer", "cancel")
           and k[0] in ("publication", "publication_failed")}
          == {("publication", "backlog"), ("publication", "cancelled"), ("publication_failed", "backlog"),
              ("publication_failed", "cancelled")}, "and defer or cancel from both stages, under the same names")
    check(not any(tr["primitive"] == "approve" for tr in doc["transitions"] if tr["from"].startswith("publication")),
          "nothing in publication is an approval: the Publisher is not a reviewer")
    off = refusal(lambda: without_publisher(doc))
    check(isinstance(off, dict), f"turning it off validates: {off}")
    back = validate(copy.deepcopy(SHIPPED))
    check({k: off[k] for k in ("stages", "transitions", "flags")} == {k: back[k] for k in ("stages", "transitions",
                                                                                         "flags")},
          "turning it off restores Audit's approvals exactly")
    check(role(off, "publisher")["active"] is False and set(off["remove_stages"]) >= {"publication",
                                                                                       "publication_failed"},
          "the Publisher stays declared, inactive, and the stages are removed explicitly")
    again = composed(off)
    check(again["stages"] == doc["stages"] and again["transitions"] == doc["transitions"]
          and role(again, "publisher")["active"] and not set(again["remove_stages"]) & {"publication",
                                                                                         "publication_failed"},
          "turning it on again reuses the same role and composes the same stages")


def test_what_cannot_be_composed_is_refused() -> None:
    doc = composed()
    check("already has a publication stage" in refusal(lambda: with_publisher(doc)), "twice is refused")
    check("has no publication stage" in refusal(lambda: without_publisher(SHIPPED)), "off when off is refused")
    no_audit = copy.deepcopy(SHIPPED)
    for s in no_audit["stages"]:
        if s["signoff"] == "audit_signoff":
            s["signoff"] = None
    no_audit["transitions"] = [tr for tr in no_audit["transitions"] if tr["action"] != "audit_sign_off"]
    no_audit["transitions"].append({"from": "audit", "to": "dat", "action": "audit_pass", "label": "Pass",
                                    "actors": ["audit"], "primitive": "move", "owner_scoped": True,
                                    "require_commit": False, "require_reason": False, "clear_signoffs": [],
                                    "allow_no_code": False, "relays_decision_of": None})
    check("no single Audit stage" in refusal(lambda: with_publisher(no_audit)), "a workflow without Audit is refused")
    split = copy.deepcopy(SHIPPED)
    stage(split, "audit")["skip_to"] = "director_review"
    check("cannot tell where approved work goes after audit" in refusal(lambda: with_publisher(split)),
          "an Audit that approves and skips to different places is refused")
    taken = copy.deepcopy(SHIPPED)
    taken["roles"].append({"name": "publisher", "label": "P", "kind": "reviewer", "active": False,
                           "capabilities": []})
    check("invalid publisher: it is a system actor" in refusal(lambda: with_publisher(taken)),
          "a role already called publisher that is something else is refused")


def test_the_validator_refuses_each_broken_shape() -> None:
    process = "invalid publisher: it is a system actor with no model, runtime, pane, session, prompt or capability"
    for field, value in (("runtime", "claude"), ("slot", 5), ("onboarding_prompt", "You publish."),
                         ("template_role", "main"), ("ephemeral", True), ("serial", True),
                         ("onboarding", "/x/prompt.md"), ("presentation_label", "Publisher")):
        def change(doc, field=field, value=value):
            for other in doc["roles"]:  # a slot is somebody's: free the one the Publisher is given
                if field == "slot" and other.get("slot") == value:
                    other["slot"] = None
            role(doc, "publisher")[field] = value
            if field in ("runtime", "slot"):
                role(doc, "publisher")["runtime"], role(doc, "publisher")["target"] = "claude", "cerulean-publisher:0.0"
        said = broken(change)
        check(process in said, f"a Publisher with {field} is refused: {said}")
    check(process in broken(lambda d: role(d, "publisher")["capabilities"].append("add_comment")),
          "a Publisher with a capability is refused")
    check(process in broken(lambda d: role(d, "publisher").update(kind="reviewer")), "a Publisher of another kind too")
    check("owns the publication stage and nothing else" in broken(
        lambda d: stage(d, "user_review")["owners"].append("publisher")), "it owns nothing else")
    extra = dict(moves(composed())[("publication", "publication_return")], action="publication_skip",
                 to="director_review", primitive="move", clear_signoffs=[])
    check("its only moves are" in broken(lambda d: d["transitions"].append(extra)), "it has no other move")
    for action, change in (("publication_published", {"owner_scoped": False}),
                           ("publication_published", {"require_commit": False}),
                           ("publication_return", {"clear_signoffs": ["audit_signoff"]}),
                           ("publication_escalate", {"require_reason": False}),
                           ("publication_retry", {"actors": ["audit"]})):
        def alter(doc, action=action, change=change):
            next(tr for tr in doc["transitions"] if tr["action"] == action).update(change)
        said = broken(alter)
        check("exactly as composed" in said, f"{action} changed by {change} is refused: {said}")
    check("is the publisher's alone, ungated" in broken(
        lambda d: stage(d, "publication").update(notify={"kind": "fixed_role", "role": "director"})),
          "publication stays silent")
    check("every approval out of Audit" in broken(
        lambda d: next(tr for tr in d["transitions"] if tr["action"] == "audit_sign_off").update(to="dat")),
          "Audit's approval cannot bypass publication")
    sneak = {"from": "inspection", "to": "dat", "action": "inspector_fast_track", "label": "Fast track",
             "actors": ["inspector"], "primitive": "approve", "owner_scoped": True, "require_commit": False,
             "require_reason": False, "clear_signoffs": [], "allow_no_code": False, "relays_decision_of": None}
    check("nothing but publication and the director's own moves reaches dat" in broken(
        lambda d: d["transitions"].append(sneak)), "nothing else may reach what publication hands on to")
    named = dict(moves(SHIPPED)[("audit", "audit_sign_off")], action="publication_published")
    check("its moves and the publication_failed stage need the publication stage" in refusal(
        lambda: validate({**SHIPPED, "transitions": SHIPPED["transitions"] + [named]})),
          "its moves cannot exist without the stage, under any actor")
    alone = {**SHIPPED, "roles": SHIPPED["roles"] + [dict(ps.PUBLISHER_DECLARATION)]}
    check("an active publisher needs its stage" in refusal(lambda: validate(alone)),
          "nor can an active Publisher")


def test_the_publisher_is_never_a_process() -> None:
    from scripts.ticket_board.provision_role_accounts import NON_PROCESS_ROLES
    from scripts import workflow_launcher

    check("publisher" in NON_PROCESS_ROLES, "provisioning makes no account for the Publisher")
    doc = composed({**SHIPPED, "project": "cerulean"})
    raw = {"project": "cerulean", "roles": [{"role": "main", "cli": ["claude"], "slot": 3}]}
    launched = workflow_launcher.project_roles(raw, doc)["roles"]
    check("publisher" not in {r.get("role") for r in launched} and {r.get("role") for r in launched}
          == {r["name"] for r in doc["roles"] if r["active"] and r.get("runtime")},
          f"the launcher's projection starts nothing for it: {[r.get('role') for r in launched]}")


def test_the_schema_carries_the_migration_exactly() -> None:
    """schema.sql's copy of pgu1004 is the migration's text, so a fresh board and an upgraded one run the same SQL."""
    migration = (ROOT / "scripts/ticket_board/migrations/pgu1004_syrd625_publisher.sql").read_text()
    body = migration.split("BEGIN;\n", 1)[1].split("-- schema.sql stops here", 1)[0].rstrip("\n") + "\n"
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    start = "-- SYRD-625: the Publisher (identical to migrations/pgu1004_syrd625_publisher.sql).\n"
    copy_ = schema.split(start, 1)[1].split("-- End of SYRD-625's Publisher.\n", 1)[0] if start in schema else ""
    check(copy_ == body, "schema.sql carries pgu1004's functions byte for byte")
    rbac = (ROOT / "scripts/ticket_board/rbac.sql").read_text()
    grants = [line for line in migration.split("-- schema.sql stops here", 1)[1].splitlines() if line.startswith("GRANT")]
    check(all(line in rbac for line in grants), "and rbac.sql grants what the migration grants")


def normal(doc: dict) -> tuple:
    """A document as a set of declarations: what it says, whatever order composition appended it in."""
    return ({st["name"]: st for st in doc["stages"]},
            sorted(json.dumps(tr, sort_keys=True) for tr in doc["transitions"]),
            doc["flags"], {r["name"]: r for r in doc["roles"]}, sorted(doc.get("remove_stages") or []))


def test_the_documentation_gate_and_the_publisher_commute() -> None:
    """SYRD-641: either may be turned on first, and each off and on again, and the document is the same."""
    from scripts.ticket_board.documentation_gate import with_documentation_gate, without_documentation_gate

    def attempt(fn):
        try:
            return fn()
        except ValueError as exc:
            return f"refused: {exc}"

    for owner, librarian in (("director", None), ("librarian", {"runtime": "claude", "template_role": "main"})):
        gate_first = attempt(lambda: with_publisher(with_documentation_gate(SHIPPED, librarian=librarian)))
        publisher_first = attempt(lambda: with_documentation_gate(with_publisher(SHIPPED), librarian=librarian))
        check(isinstance(publisher_first, dict),
              f"{owner}: the gate can be turned on where the Publisher already runs: {publisher_first}")
        check(isinstance(gate_first, dict) and normal(gate_first) == normal(publisher_first),
              f"{owner}: either order composes the same document")
        returns = [tr for tr in publisher_first["transitions"]
                   if tr["action"] in (ps.RETURN, ps.KICK_BACK)]
        check(len(returns) == 2 and all(tr["clear_signoffs"] == sorted(
                  n for n, f in publisher_first["flags"].items() if f["kind"] == "signoff")
                  and "documentation_signoff" in tr["clear_signoffs"] for tr in returns),
              f"{owner}: the Publisher's returns clear the gate's sign-off too: {returns}")
        doc = publisher_first
        for cycle in range(3):  # each off and on again, alternately, three times
            off = attempt(lambda: without_documentation_gate(doc))
            check(isinstance(off, dict) and all("documentation_signoff" not in tr["clear_signoffs"]
                                               for tr in off["transitions"]),
                  f"{owner} cycle {cycle}: the gate off leaves no return clearing a sign-off it no longer has: {off}")
            doc = attempt(lambda: with_documentation_gate(off, librarian=librarian))
            check(isinstance(doc, dict) and normal(doc) == normal(publisher_first),
                  f"{owner} cycle {cycle}: the gate on again gives back the same document")
            off = attempt(lambda: without_publisher(doc))
            check(isinstance(off, dict), f"{owner} cycle {cycle}: the Publisher off: {off}")
            doc = attempt(lambda: with_publisher(off))
            check(isinstance(doc, dict) and normal(doc) == normal(publisher_first),
                  f"{owner} cycle {cycle}: the Publisher on again gives back the same document")
        # Fail closed, still: a return that does not clear every sign-off is refused, never repaired by validation.
        stale = json.loads(json.dumps(publisher_first))
        next(tr for tr in stale["transitions"] if tr["action"] == ps.RETURN)["clear_signoffs"].remove(
            "documentation_signoff")
        check("exactly as composed" in str(refusal(lambda: validate(stale))),
              f"{owner}: a stale sign-off clearing is still refused")


def test_the_engine_reads_git_as_it_answers() -> None:
    """The Publisher's small judgements, each on git's own words and git's own formats."""
    import subprocess
    import tempfile
    from scripts.ticket_board import publisher as pb

    said = {
        "! [rejected]        x -> main (stale info)": "race",
        "! [rejected]        x -> main (non-fast-forward)": "race",
        "! [remote rejected] x -> main (pre-receive hook declined)": "policy",
        "remote: error: GH006: Protected branch update failed for refs/heads/main.": "policy",
        "git@github.com: Permission denied (publickey).": "credential",
        "remote: Permission to o/r.git denied to someone.": "credential",
        "fatal: unable to access 'https://x/': The requested URL returned error: 403": "credential",
        "fatal: unable to access 'https://x/': Could not resolve host: github.com": "transient",
        "fatal: the remote end hung up unexpectedly": "transient",
        "git push timed out after 180s": "transient",
        "fatal: bad object deadbeef": "helper",
    }
    check({text: pb.classify(text) for text in said} == said,
          f"each failure is named by what git said: {[(t, pb.classify(t)) for t in said if pb.classify(t) != said[t]]}")
    result = subprocess.CompletedProcess(["git"], 1, "", "remote: Counting objects: 3\nTo /x\nerror: hook said no\n"
                                         "fatal: could not push\n")
    check(pb.git_said(result) == "error: hook said no / fatal: could not push",
          f"git's words are quoted whole, without its progress: {pb.git_said(result)}")
    check(pb.git_said(subprocess.CompletedProcess(["git"], 7, "", "")) == "git exited 7 with no output",
          "silence is said as silence")
    env = pb.git_environment({"HOME": "/h", "PATH": "/bin", "GIT_DIR": "/elsewhere", "GIT_SSH_COMMAND": "evil",
                              "GIT_CONFIG_PARAMETERS": "'url.x.insteadof=y'"})
    check(env["HOME"] == "/h" and env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
          and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
          and "GIT_DIR" not in env and "GIT_CONFIG_PARAMETERS" not in env,
          f"git runs with none of a role's configuration and never prompts: {env}")
    check(pb.state_root("syrd", {"XDG_STATE_HOME": "/s", "HOME": "/h"}) == Path("/s/switchyard/publisher/syrd")
          and pb.state_root("syrd", {"HOME": "/h"}) == Path("/h/.local/state/switchyard/publisher/syrd"),
          "the Publisher's scratch space is the account's own state directory")
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "b.bundle"
        head, base = "a" * 40, "b" * 40
        for header, shape in (
                (f"# v2 git bundle\n-{base} base\n{head} {pb.CANDIDATE_REF}\n\nPACK", (head, base)),
                (f"# v3 git bundle\n@object-format=sha1\n-{base} base\n{head} {pb.CANDIDATE_REF}\n\nPACK", (head, base)),
                (f"# v2 git bundle\n-{base} base\n-{'c' * 40} other\n{head} {pb.CANDIDATE_REF}\n\nPACK", None),
                (f"# v2 git bundle\n{head} {pb.CANDIDATE_REF}\n\nPACK", None),
                (f"# v2 git bundle\n-{base} base\n{head} {pb.CANDIDATE_REF}\n{head} refs/heads/x\n\nPACK", None),
                (f"# v2 git bundle\n-{base} base\n{head} refs/heads/main\n\nPACK", None),
                ("not a bundle\n\n", None)):
            bundle.write_text(header)
            check(pb.Attempt.bundle_shape(bundle) == shape, f"a bundle is one head on one prerequisite: {header!r}")
        check(pb.Attempt.bundle_shape(Path(tmp) / "missing") is None, "a missing bundle has no shape")


def exercise_command(cluster, board: Board) -> None:
    """`switchyard publisher`, against the board and a real tenant projection."""
    launcher_dir = cluster.root / "launcher"
    launcher_dir.mkdir()
    project_dir = cluster.root / "project"
    (project_dir / "docs" / "onboarding").mkdir(parents=True)
    config_path = launcher_dir / "cerulean.json"
    config_path.write_text(json.dumps({
        "project": "cerulean", "layout": "layout.json", "board_url": board.base,
        "roles": [{"role": "main", "cli": ["/nonexistent/claude"], "slot": 3}],
        "worktree_base": str(cluster.root / "worktrees"), "repository": str(project_dir),
    }))
    t.TEST_WRITE_TOKEN = board.server.write_token
    t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                {"document": validate(copy.deepcopy(SHIPPED)), "expected_revision": 0, "dry_run": False},
                caller="director")
    os.environ["TICKET_BOARD_WRITE_TOKEN"] = board.server.write_token
    os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
    importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
    importlib.reload(importlib.import_module("scripts.workflow_manage"))
    launcher = importlib.import_module("scripts.team_launcher")
    check("publisher" in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS, "the Director turns it on without root")
    previous = launcher.DEFAULT_CONFIG_DIR
    launcher.DEFAULT_CONFIG_DIR = launcher_dir
    try:
        def run(*args: str, command: str = "publisher") -> tuple[object, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = launcher.switchyard_main([command, *args])
                except SystemExit as exc:
                    code = exc.code
                except Exception as exc:  # a fault in the command is a failed check, not a crash
                    code = f"{type(exc).__name__}: {exc}"
            return code, out.getvalue(), err.getvalue()

        before = board.document()
        code, out, _ = run("cerulean", "enable", "--dry-run")
        check(code == 0 and '"publication_published"' in out and board.document() == before,
              f"a dry run shows the document and changes nothing: {code}")
        code, _, err = run("cerulean", "enable")
        stored = board.document()
        check(code == 0 and stage(stored, "publication")["owners"] == ["publisher"]
              and moves(stored)[("audit", "audit_sign_off")]["to"] == "publication",
              f"enable puts the publication stage after Audit on the board: {code} {err[-300:]}")
        launched = {r["role"] for r in json.loads(config_path.read_text())["roles"]}
        check("publisher" not in launched
              and launched == {r["name"] for r in stored["roles"] if r["active"] and r.get("runtime")},
              f"and starts nothing for it: no launcher entry, no pane, no session: {sorted(launched)}")
        projected = json.loads((launcher_dir / "workflow.json").read_text())
        check(stage(projected, "publication")["owners"] == ["publisher"], "the tenant's copy has it too")
        code, _, err = run("cerulean", "enable")
        check(code != 0 and "already has a publication stage" in err, f"twice is refused: {err[-200:]}")
        code, _, err = run("disable", "--project", "cerulean")
        stored = board.document()
        check(code == 0 and "publication" not in [s["name"] for s in stored["stages"]]
              and moves(stored)[("audit", "audit_sign_off")]["to"] == "dat" and not role(stored, "publisher")["active"],
              f"disable puts Audit's approvals back and turns the Publisher off: {code} {err[-300:]}")
        # SYRD-641: the documentation gate and the Publisher, by their real commands, in both orders and cycles.
        sequence = [("publisher", "enable"), ("documentation-gate", "enable"), ("documentation-gate", "disable"),
                    ("documentation-gate", "enable"), ("publisher", "disable"), ("publisher", "enable")]
        documents = {}
        for command, action in sequence:
            code, _, err = run("cerulean", action, command=command)
            check(code == 0, f"switchyard {command} cerulean {action}, with the other as it stands: {code} {err[-400:]}")
            documents[(command, action, len(documents))] = board.document()
        both = [doc for key, doc in documents.items() if key[2] in (1, 3, 5)]
        check(all(normal(doc) == normal(both[0]) for doc in both)
              and stage(both[0], "documentation")["owners"] == ["director"]
              and stage(both[0], "publication")["owners"] == ["publisher"],
              "the board holds the same document whichever was turned on last")
        gate_alone = documents[("documentation-gate", "disable", 2)]
        check("documentation" not in [st["name"] for st in gate_alone["stages"]]
              and all("documentation_signoff" not in tr["clear_signoffs"] for tr in gate_alone["transitions"]),
              "the gate turned off on the Publisher's board leaves its returns clearing only what remains")
        for command in ("publisher", "documentation-gate"):
            check(run("cerulean", "disable", command=command)[0] == 0, f"{command} off again")
    finally:
        launcher.DEFAULT_CONFIG_DIR = previous


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    with temporary_cluster(prefix="syrd625c-", shutdown="immediate") as cluster:
        for db, migrate in (("schema_copy", False), ("migration_copy", True)):
            board = Board(cluster, db, migrate=migrate)
            try:
                for doc, what in ((composed(), "the Publisher"),
                                  (without_publisher(composed()), "the Publisher turned off")):
                    said = board.sql_validates(doc)
                    check(said == "", f"{db}: the board's own validator accepts {what}: {said[-300:]}")
            finally:
                board.close()
        board = Board(cluster, "command", migrate=True)
        try:
            exercise_command(cluster, board)
        finally:
            board.close()
    print(f"publisher_command_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
