#!/usr/bin/env python3
"""SYRD-624: what the documentation gate and its Librarian may be, how they are turned on, and what they run.

* Each rule of the gate's shape is enforced by workflow_config, and the board's
  own validate_declared_workflow, asked directly on a schema.sql board and on a
  board after the real ticket-board-migrate, accepts both composed documents:
  the gate needs no migration.
* `switchyard documentation-gate enable|disable`, against a board and a real
  tenant projection: the Director-owned gate creates no role; a Librarian lands
  detached, with its own runtime and prompt, under the project account.
* The Librarian's whole path with the real programs: a bare remote, the
  Librarian's clone, the board's commit cache as a clone of the same remote,
  `switchyard-publish-candidate` as the Librarian (which chooses the ticket's
  documentation ref and refuses any other) and the real `ticket-board-write`
  `documentation-sign-off` deriving the same ref; the CLI refuses the
  Librarian everything else before asking the board.
* The skills: the Librarian's is installed for every CLI and pointed at the
  Librarian alone; `switchyard new` copies the Librarian's guide; the guide,
  skill and prompt teach what the ticket requires.
* `switchyard new`'s choice: the artifact record, the role plan's entry, the
  summary lines and the composed plan, with Router and without.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board import documentation_gate as gate  # noqa: E402
from scripts.ticket_board.documentation_gate import (  # noqa: E402
    LIBRARIAN_PROMPT, candidate_ref, with_documentation_gate, without_documentation_gate)
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
SCHEMA = ROOT / "scripts/ticket_board/schema.sql"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def librarian_doc() -> dict:
    return with_documentation_gate(copy.deepcopy(SHIPPED), librarian={"runtime": "claude"})


def director_doc() -> dict:
    return with_documentation_gate(copy.deepcopy(SHIPPED))


def role(doc: dict, name: str) -> dict:
    return next(r for r in doc["roles"] if r["name"] == name)


def transition(doc: dict, action: str) -> dict:
    return next(x for x in doc["transitions"] if x["action"] == action)


def stage(doc: dict, name: str) -> dict:
    return next(s for s in doc["stages"] if s["name"] == name)


def refusal(fn) -> str:
    try:
        fn()
    except (ValueError, SystemExit) as exc:
        return str(exc)
    return ""


def broken(make, change) -> dict:
    doc = copy.deepcopy(make())
    change(doc)
    return doc


#: Shapes the gate may not take, each with the words its refusal must say.
VIOLATIONS = (
    ("a Librarian holding publication", librarian_doc,
     lambda d: role(d, "librarian")["capabilities"].append("request_publication"), "holds no capability but add_comment"),
    ("a Librarian resolving publication", librarian_doc,
     lambda d: role(d, "librarian")["capabilities"].append("resolve_publication"), "holds no capability but add_comment"),
    ("a Librarian that reassigns", librarian_doc,
     lambda d: role(d, "librarian")["capabilities"].append("reassign"), "holds no capability but add_comment"),
    ("a Librarian that edits", librarian_doc,
     lambda d: role(d, "librarian")["capabilities"].append("edit_fields"), "holds no capability but add_comment"),
    ("a Librarian that is an implementer", librarian_doc,
     lambda d: role(d, "librarian").update(kind="implementer"), "the librarian role is a reviewer"),
    ("a Librarian holding a serial slot", librarian_doc,
     lambda d: role(d, "librarian").update(serial=True), "never holds a serial slot"),
    ("a Librarian owning Audit", librarian_doc,
     lambda d: stage(d, "audit")["owners"].append("librarian"), "owns the documentation stage and nothing else"),
    ("a Librarian signing off Audit", librarian_doc,
     lambda d: transition(d, "audit_sign_off")["actors"].append("librarian"), "its only moves are the documentation"),
    ("a Librarian cancelling", librarian_doc,
     lambda d: next(x for x in d["transitions"] if x["from"] == "documentation" and x["action"] == "cancel")
     ["actors"].append("librarian"), "its only moves are the documentation"),
    ("a sign-off that keeps Audit's verdict", librarian_doc,
     lambda d: transition(d, gate.SIGN_OFF)["clear_signoffs"].remove("audit_signoff"), "exactly as composed"),
    ("a sign-off without a commit", librarian_doc,
     lambda d: transition(d, gate.SIGN_OFF).update(require_commit=False), "exactly as composed"),
    ("a no-change approval without a reason", librarian_doc,
     lambda d: transition(d, gate.NO_CHANGE).update(require_reason=False), "exactly as composed"),
    ("a decision anybody may take", librarian_doc,
     lambda d: transition(d, gate.NO_CHANGE).update(owner_scoped=False), "exactly as composed"),
    ("a sign-off that skips Inspection and Audit", librarian_doc,
     lambda d: transition(d, gate.SIGN_OFF).update(to="director_review"), "exactly as composed"),
    ("a gate that is on unless turned off", librarian_doc,
     lambda d: d["flags"][gate.GATE].update(default=True), "off unless the director turns it on"),
    ("a submission around the gate", librarian_doc,
     lambda d: transition(d, "submit_to_audit").update(to="inspection"), "every submission out of Implementation enters it"),
    ("a stage owned by an implementer", director_doc,
     lambda d: stage(d, "documentation").update(owners=["app"]), "the librarian's alone, or the director's"),
    ("a Director-owned stage with a sign-off", director_doc,
     lambda d: stage(d, "documentation").update(signoff=gate.SIGNOFF), "the director holds no sign-off"),
    ("the gate's decisions without its stage", lambda: copy.deepcopy(SHIPPED),
     lambda d: d["transitions"].append({**copy.deepcopy(SHIPPED["transitions"][0]), "from": "inspection",
                                        "to": "audit", "action": gate.NO_CHANGE, "actors": ["inspector"]}),
     "its decisions need the documentation stage"),
)


def test_the_shapes_compose_validate_and_turn_off() -> None:
    lib, direct = librarian_doc(), director_doc()
    names = [s["name"] for s in lib["stages"]]
    check(names.index("documentation") == names.index("in_progress") + 1,
          "the stage is directly after Implementation, before Inspection and Audit")
    check(stage(lib, "documentation")["skip_to"] == "inspection" and stage(lib, "documentation")["gate"] == gate.GATE,
          "gated, and skipping to where submissions went")
    check({x["action"]: x["to"] for x in lib["transitions"] if x["from"] == "in_progress" and x["to"] in
           ("documentation", "inspection", "audit")} ==
          {"submit_to_audit": "documentation", "submit_to_inspection": "documentation",
           "submit_to_audit_without_commit": "documentation"},
          "every submission enters it")
    librarian = role(lib, "librarian")
    check(librarian["capabilities"] == ["add_comment"] and librarian["kind"] == "reviewer"
          and librarian["slot"] is None and librarian["ephemeral"] is True and librarian["serial"] is False
          and librarian["onboarding_prompt"] == LIBRARIAN_PROMPT and librarian["target"] == "cerulean-librarian:0.0",
          f"a detached, ephemeral reviewer holding only add_comment: {librarian}")
    check(sorted(x["action"] for x in lib["transitions"] if "librarian" in x["actors"]) == sorted(gate.GATE_ACTIONS),
          "the Librarian is the actor of its three decisions and nothing else")
    sign = transition(lib, gate.SIGN_OFF)
    check(sign["primitive"] == "approve" and sign["require_commit"] and sorted(sign["clear_signoffs"]) ==
          ["audit_signoff", "inspector_signoff", "user_signoff"], f"the sign-off clears every later sign-off: {sign}")
    check(transition(lib, gate.NO_CHANGE)["require_reason"] and not transition(lib, gate.NO_CHANGE)["require_commit"],
          "no change needs a reason and no commit")
    check(transition(lib, gate.KICK_BACK)["primitive"] == "return" and transition(lib, gate.KICK_BACK)["to"] == "in_progress",
          "the kick-back returns to Implementation")
    check(not any(r["name"] == "librarian" for r in direct["roles"]), "the Director-owned gate creates no role")
    check(stage(direct, "documentation")["owners"] == ["director"] and stage(direct, "documentation")["signoff"] is None
          and all(x["primitive"] != "approve" for x in direct["transitions"] if x["from"] == "documentation"),
          "the Director owns it through moves, holding no sign-off")
    for doc, label in ((lib, "librarian"), (direct, "director")):
        off = without_documentation_gate(doc)
        check("documentation" not in [s["name"] for s in off["stages"]] and gate.GATE not in off["flags"]
              and off["remove_stages"][-1] == "documentation"
              and {x["action"]: x["to"] for x in off["transitions"] if x["from"] == "in_progress" and "submit" in x["action"]}
              == {x["action"]: x["to"] for x in validate(copy.deepcopy(SHIPPED))["transitions"]
                  if x["from"] == "in_progress" and "submit" in x["action"]},
              f"{label}: turning it off puts every submission back where it went")
    check(role(without_documentation_gate(lib), "librarian")["active"] is False, "and keeps the Librarian, inactive")
    again = with_documentation_gate(without_documentation_gate(lib), librarian={"runtime": "codex"})
    check(role(again, "librarian")["active"] and role(again, "librarian")["runtime"] == "codex",
          "the same Librarian turns back on")
    check("already has a documentation stage" in refusal(lambda: with_documentation_gate(lib)),
          "enabling twice changes nothing")
    check("has no documentation stage" in refusal(lambda: without_documentation_gate(copy.deepcopy(SHIPPED))),
          "disabling where there is none changes nothing")
    check("runtime must be one of" in refusal(lambda: with_documentation_gate(SHIPPED, librarian={"runtime": "vim"})),
          "a Librarian needs a real runtime")
    check("is not a role of this workflow" in refusal(lambda: with_documentation_gate(
        SHIPPED, librarian={"runtime": "claude", "template_role": "nobody"})), "and a real template role")
    from scripts.ticket_board.router_setup import with_router
    both = with_documentation_gate(with_router(copy.deepcopy(SHIPPED), runtime="claude"), librarian={"runtime": "claude"})
    check(role(both, "router")["active"] and role(both, "librarian")["active"], "Router and the gate compose")


def test_python_refuses_each_broken_shape() -> None:
    for name, make, change, words in VIOLATIONS:
        said = refusal(lambda: validate(broken(make, change)))
        check(words in said, f"workflow_config: {name}: {said!r}")


def test_the_librarian_is_taught_what_the_ticket_requires() -> None:
    skill = (ROOT / "skills/switchyard-librarian/SKILL.md").read_text()
    guide = (ROOT / "docs/onboarding/switchyard-librarian-guide.md").read_text()
    flat = lambda text: " ".join(text.split())  # noqa: E731 - prose wraps where it likes
    check(skill.startswith("---\nname: switchyard-librarian\n") and "ONLY when TICKET_BOARD_CALLER_ROLE is exactly librarian"
          in skill, "the skill's frontmatter names it and scopes it to the Librarian")
    for text, where in ((skill, "skill"), (guide, "guide")):
        for words in ("CLI help", "onboarding", "examples", "stale", "contradict", "documentation-no-change",
                      "documentation-sign-off", "documentation-kick-back", "sole parent", "switchyard-publish-candidate",
                      "every later sign-off", "Routing", "Publication", "Implementation", "acceptance",
                      "never a reason to push anywhere else", "force-push"):
            check(words in flat(text), f"the Librarian's {where} covers {words!r}")
    for words in ("switchyard-librarian skill", "documentation-no-change", "documentation-kick-back",
                  "documentation-sign-off", "sole parent", "switchyard-publish-candidate", "Never change product code",
                  "You do not route"):
        check(words in flat(LIBRARIAN_PROMPT), f"the prompt says {words!r}")
    check(len(LIBRARIAN_PROMPT) <= 16384, "the prompt fits a stored onboarding prompt")
    from scripts.ticket_board import board_skill
    check(board_skill.LIBRARIAN_SKILL in board_skill.CANONICAL_SKILLS, "the Librarian's skill is canonical")
    check([s.name for s in board_skill.skills_for_role("librarian")] == ["switchyard-board", "switchyard-librarian"],
          "the Librarian is pointed at the shared skill and its own")
    for name in ("director", "app", "audit", "router", "inspector"):
        check(board_skill.LIBRARIAN_SKILL not in board_skill.skills_for_role(name), f"{name} is not pointed at it")
    with tempfile.TemporaryDirectory(prefix="syrd624-skill.") as home:
        paths = dict(board_skill.adapter_paths(Path(home), board_skill.LIBRARIAN_SKILL))
        check(sorted(paths) == sorted(runtime for runtime, _ in board_skill.SKILL_ADAPTERS) and all(
            str(p).endswith("switchyard-librarian/SKILL.md") for p in paths.values()),
            f"it is installed for every supported CLI: {paths}")
        done = subprocess.run([sys.executable, str(ROOT / "scripts/switchyard-board-skill"), "install", "--home", home],
                              capture_output=True, text=True, timeout=120)
        check(done.returncode == 0 and all(p.exists() and p.read_text().startswith("---\nname: switchyard-librarian")
                                           for p in paths.values()),
              f"and the real installer puts it in every CLI's tree: {done.stdout[-400:]} {done.stderr[-400:]}")
    from scripts import team_launcher
    check("switchyard-librarian-guide.md" in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES,
          "`switchyard new` copies the Librarian's guide into every project")


def test_switchyard_new_records_and_composes_the_choice() -> None:
    from scripts import documentation_onboarding as onboarding
    from scripts.new_project_selection import RoleSelection

    path = Path("/x/p.project.json")
    roles = ["director", "main", "ops", "audit", "designer"]
    check(onboarding.artifact_documentation(None, path=path, role_names=roles) == (), "no field is off")
    check(onboarding.artifact_documentation({"gate": "off"}, path=path, role_names=roles) == (), "off is off")
    check(onboarding.artifact_documentation({"gate": "director"}, path=path, role_names=roles) == (("gate", "director"),),
          "the Director-owned gate")
    lib = onboarding.artifact_documentation({"gate": "librarian", "runtime": "claude", "model": "m", "effort": "low"},
                                            path=path, role_names=roles)
    check(lib == (("gate", "librarian"), ("runtime", "claude"), ("model", "m"), ("effort", "low")), f"a Librarian: {lib}")
    for raw, words in (({"gate": "on"}, "must be one of"), ({"gate": "director", "runtime": "claude"}, "only a Librarian takes"),
                       ({"gate": "librarian"}, "runtime"), ({"gate": "director", "x": 1}, "unknown key"),
                       ("director", "must be a JSON object")):
        check(words in refusal(lambda: onboarding.artifact_documentation(raw, path=path, role_names=roles)),
              f"a malformed record is refused: {raw!r}")
    check("which the Librarian needs" in refusal(lambda: onboarding.artifact_documentation(
        {"gate": "librarian", "runtime": "claude"}, path=path, role_names=[*roles, "librarian"])),
        "a role already named librarian is refused")
    plan = [RoleSelection("director", "claude"), RoleSelection("main", "codex"), RoleSelection("router", "claude")]
    rest, record = onboarding.split_role_plan([*plan, RoleSelection(onboarding.GATE_MARKER, "director")])
    check(rest == tuple(plan) and record == (("gate", "director"),), "the Director's gate leaves the role plan")
    rest, record = onboarding.split_role_plan([*plan, RoleSelection("librarian", "codex", "gpt", "high")])
    check(rest == tuple(plan) and record == (("gate", "librarian"), ("runtime", "codex"), ("model", "gpt"),
                                             ("effort", "high")), "so does the Librarian, as its record")
    check(onboarding.split_role_plan(plan) == (tuple(plan), ()), "and nothing chosen is off")
    check("off" in onboarding.summary_line(()) and "decided by the Director" in onboarding.summary_line(
        (("gate", "director"),)) and "codex -> gpt -> effort high" in onboarding.summary_line(record),
        "each choice is said before anything is created")
    check(onboarding.with_librarian_cli((("main", "codex"),), record) == (("main", "codex"), ("librarian", "codex"))
          and onboarding.with_librarian_cli((("main", "codex"),), (("gate", "director"),)) == (("main", "codex"),),
          "a Librarian's runtime goes through the CLI checks; the Director's gate adds none")
    from scripts import team_launcher as launcher
    from scripts.project_design_payload import project_design_artifact_payload
    answers = iter(["3", "claude", "", ""])
    out: list[str] = []
    chosen = onboarding.prompt_documentation(
        ["main", "ops"], runner=None, owner_args=(), unverified_because="not yet", input_func=lambda _p: next(answers),
        print_func=out.append)
    check(isinstance(chosen, launcher.RoleSelection) and chosen.role == "librarian" and chosen.cli == "claude",
          f"choosing a Librarian asks its runtime the guided way: {chosen} {out[-3:]}")
    chosen = onboarding.prompt_documentation(["main"], runner=None, owner_args=(), unverified_because="",
                                             input_func=lambda _p: "", print_func=out.append)
    check(chosen is None, "the default is off")
    check(any("Documentation gate (optional)" in line for line in out), "under a plainly labelled menu")
    payload = project_design_artifact_payload(dataclass_replace(sample_artifact(), documentation=(("gate", "director"),)))
    check(payload["project"]["documentation"] == {"gate": "director"}, "the artifact records it")
    check("documentation" not in project_design_artifact_payload(sample_artifact())["project"],
          "and writes nothing when it is off")


def sample_artifact():
    from scripts.project_design_payload import ProjectDesignArtifact

    return ProjectDesignArtifact(
        project="cer", project_name="Cer", ticket_prefix="CER", owner_user="cer-agent", repository=Path("/r"),
        remote="origin", default_branch="main", worktree_policy="shared", design_document=Path("/r/d.md"),
        implementer_roles=("main", "ops"), audit_roles=("audit",),
        role_clis=(("director", "claude"), ("main", "codex"), ("ops", "codex"), ("audit", "claude")),
        include_designer=False, include_audit=True, push_policy="director-main-only", gates={}, capability_grants={})


def dataclass_replace(value, **changes):
    import dataclasses

    return dataclasses.replace(value, **changes)


class Board:
    def __init__(self, cluster, db: str, *, migrate: bool) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(self.admin, SCHEMA.read_text())
        if t.psql(self.admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
            t.create_roles(self.admin)
        t.psql(self.admin, (ROOT / "scripts/ticket_board/rbac.sql").read_text())
        if migrate:
            done = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                  env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin}, timeout=300)
            check(done.returncode == 0, done.stderr[-800:])
            t.psql(self.admin, (ROOT / "scripts/ticket_board/rbac.sql").read_text())
        frames, assets = cluster.root / f"{db}-frames", cluster.root / f"{db}-assets"
        self.app = t.TicketBoardApp(frames, assets, project="cerulean", ticket_prefix="PGU",
                                    database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
        self.server = t.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=t.QuietNotifier())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def sql_validates(self, document: dict) -> str:
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"doc={json.dumps(document)}",
                               "-d", self.admin], input="SELECT ticket_board.validate_declared_workflow(:'doc'::jsonb);\n",
                              capture_output=True, text=True)
        return "" if done.returncode == 0 else done.stderr

    def document(self) -> dict:
        return json.loads(t.psql(self.admin, "SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def exercise_sql_validator(board: Board, label: str) -> None:
    for doc, what in ((librarian_doc(), "a Librarian's gate"), (director_doc(), "a Director-owned gate"),
                      (without_documentation_gate(librarian_doc()), "the gate turned off")):
        said = board.sql_validates(doc)
        check(said == "", f"{label}: the board's own validator accepts {what}, with no migration: {said[-300:]}")


def exercise_command(cluster, board: Board) -> None:
    """`switchyard documentation-gate`, against the board and a real tenant projection."""
    launcher_dir = cluster.root / "launcher"
    launcher_dir.mkdir()
    project_dir = cluster.root / "project"
    (project_dir / "docs" / "onboarding").mkdir(parents=True)
    (cluster.root / "cerulean.project.json").write_text(json.dumps({"project": {"repository": str(project_dir)}}))
    config_path = launcher_dir / "cerulean.json"
    config_path.write_text(json.dumps({
        "project": "cerulean", "layout": "layout.json", "board_url": board.base,
        "roles": [{"role": "main", "cli": ["/nonexistent/claude"], "slot": 3, "model": "the-template-model"}],
        "worktree_base": str(cluster.root / "worktrees"), "repository": str(project_dir),
    }))
    t.TEST_WRITE_TOKEN = board.server.write_token
    t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                {"document": validate(copy.deepcopy(SHIPPED)), "expected_revision": 0, "dry_run": False}, caller="director")
    os.environ["TICKET_BOARD_WRITE_TOKEN"] = board.server.write_token
    os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
    importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
    importlib.reload(importlib.import_module("scripts.workflow_manage"))
    launcher = importlib.import_module("scripts.team_launcher")
    check("documentation-gate" in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS, "the Director turns the gate on without root")
    previous = launcher.DEFAULT_CONFIG_DIR
    launcher.DEFAULT_CONFIG_DIR = launcher_dir
    try:
        def run(*args: str) -> tuple[object, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = launcher.switchyard_main(["documentation-gate", *args])
                except SystemExit as exc:
                    code = exc.code
                except Exception as exc:  # a fault in the command is a failed check, not a crash
                    code = f"{type(exc).__name__}: {exc}"
            return code, out.getvalue(), err.getvalue()

        before = board.document()
        code, out, _ = run("cerulean", "enable", "--dry-run")
        check(code == 0 and '"documentation_no_change"' in out and board.document() == before,
              f"a dry run shows the document and changes nothing: {code}")
        code, _, err = run("cerulean", "enable", "--template-role", "main")
        check(code != 0 and "needs --runtime" in err, f"a template role is for a Librarian: {err[-200:]}")
        code, _, err = run("cerulean", "enable")
        stored = board.document()
        check(code == 0 and stage(stored, "documentation")["owners"] == ["director"]
              and not any(r["name"] == "librarian" for r in stored["roles"]),
              f"enable without a runtime gives the decision to the Director and creates no role: {code} {err[-300:]}")
        check(not any(r["role"] == "librarian" for r in json.loads(config_path.read_text())["roles"]),
              "and no launcher entry, so no session and no cost")
        code, _, err = run("cerulean", "enable", "--runtime", "codex")
        check(code != 0 and "already has a documentation stage, owned by director" in err,
              f"changing who decides means disabling first: {err[-200:]}")
        code, _, err = run("disable", "--project", "cerulean")
        check(code == 0 and "documentation" not in [s["name"] for s in board.document()["stages"]],
              f"disable removes it: {code} {err[-300:]}")
        code, _, err = run("cerulean", "enable", "--runtime", "codex")
        stored = board.document()
        check(code == 0 and role(stored, "librarian")["active"] and role(stored, "librarian")["runtime"] == "codex"
              and stage(stored, "documentation")["owners"] == ["librarian"],
              f"enable --runtime gives it to a Librarian: {code} {err[-300:]}")
        entry = next((r for r in json.loads(config_path.read_text())["roles"] if r["role"] == "librarian"), None)
        check(entry is not None and entry.get("detached") is True and "slot" not in entry
              and entry.get("cli") == ["codex"] and entry.get("model") == "the-template-model"
              and "run_as_user" not in entry,
              f"the launcher runs the Librarian detached, on its own runtime, as the project account: {entry}")
        env = (entry or {}).get("env") or {}
        check(env.get("TICKET_BOARD_ROLE_ONBOARDING_PROMPT") == LIBRARIAN_PROMPT,
              f"its fresh session starts with its prompt: {sorted(env)}")
        projected = json.loads((launcher_dir / "workflow.json").read_text())
        check(stage(projected, "documentation")["owners"] == ["librarian"], "the tenant's copy has the gate too")
        from scripts import role_runtime
        cheaper = role_runtime.document_with_runtime(stored, role="librarian", runtime="claude")
        check(role(cheaper, "librarian")["runtime"] == "claude" and validate(copy.deepcopy(cheaper)),
              "set-role-runtime moves the Librarian like any role")
        code, _, err = run("cerulean", "disable")
        launched = json.loads(config_path.read_text())
        check(code == 0 and not role(board.document(), "librarian")["active"]
              and not any(r["role"] == "librarian" for r in launched["roles"]),
              f"disable turns the Librarian off on the board and in the launcher: {code} {err[-300:]}")
    finally:
        launcher.DEFAULT_CONFIG_DIR = previous


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def exercise_librarian_path(cluster, board: Board) -> None:
    """The Librarian's whole path with the real publisher and the real CLI."""
    remote, work, cache = cluster.root / "remote.git", cluster.root / "librarian-wt", cluster.root / "commit-cache"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, env={**os.environ, **GIT_ENV})
    (work / "README.md").write_text("base\n")
    git(work, "add", "README.md")
    git(work, "commit", "-q", "-m", "base")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    (work / "tool.py").write_text("print('new flag')\n")
    git(work, "add", "tool.py")
    git(work, "commit", "-q", "-m", "the implementer's candidate")
    candidate = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/roles/app/pgu-951")
    subprocess.run(["git", "clone", "-q", "--mirror", str(remote), str(cache)], check=True)
    git(cache, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(cache, "fetch", "-q", "origin")
    board.app.commit_git_dirs = (cache,)
    board.app.commit_git_dir = cache
    revision = int(t.psql(board.admin, "SELECT revision FROM ticket_board.workflow_configuration;"))
    t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                {"document": with_documentation_gate(validate(copy.deepcopy(SHIPPED)), librarian={"runtime": "claude"})
                 if not any(s["name"] == "documentation" for s in board.document()["stages"]) else board.document(),
                 "expected_revision": revision, "dry_run": False}, caller="director")
    if not role(board.document(), "librarian")["active"]:
        revision = int(t.psql(board.admin, "SELECT revision FROM ticket_board.workflow_configuration;"))
        t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                    {"document": with_documentation_gate(without_documentation_gate(board.document()),
                                                         librarian={"runtime": "claude"}),
                     "expected_revision": revision, "dry_run": False}, caller="director")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": os.environ.get("HOME", "/tmp"),
           "TICKET_BOARD_URL": board.base, "TICKET_BOARD_WRITE_TOKEN": board.server.write_token, **GIT_ENV}

    def run(*args: str, caller: str = "librarian", cwd: Path = work) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts" / args[0]), *args[1:]], capture_output=True, text=True, timeout=120,
                              env={**env, "TICKET_BOARD_CALLER_ROLE": caller}, cwd=str(cwd), stdin=subprocess.DEVNULL)

    def where(ticket: str) -> str:
        return t.psql(board.admin, f"SELECT state || '/' || assignee || '/' || commit_hash FROM ticket_board.tickets "
                                   f"WHERE id = '{ticket}';")

    ticket = "PGU-951"
    t.seed_postgres_ticket(board.admin, ticket, title=ticket, state="analysis", assignee="director")
    check(run("ticket-board-write", "route", ticket, "--state", "in_progress", "--assignee", "app",
              caller="director").returncode == 0, "routed")
    flag = run("ticket-board-write", "require-documentation", ticket, caller="director")
    check(flag.returncode == 0 and json.loads(t.psql(board.admin, f"SELECT workflow_flags FROM ticket_board.tickets "
                                                                  f"WHERE id = '{ticket}';"))[gate.GATE] is True,
          f"`require-documentation` turns the gate on: {flag.stderr[-300:]}")
    refused = run("ticket-board-write", "require-documentation", ticket, caller="app")
    check(refused.returncode != 0 and "only director may set gates" in refused.stderr, "and only for the Director")
    submitted = run("ticket-board-write", "submit-to-audit", ticket, "--commit-hash", candidate, caller="app")
    check(submitted.returncode == 0 and where(ticket) == f"documentation/librarian/{candidate}",
          f"submitted, it waits for the Librarian: {submitted.stderr[-300:]} {where(ticket)}")

    for args in (("route", ticket, "--state", "analysis", "--assignee", "director"),
                 ("set-blockers", ticket, "--blocked-by", "PGU-1", "--blocked-reason", "x"),
                 ("submit-to-audit", ticket, "--commit-hash", candidate),
                 ("request-publication", ticket, "--ref", "roles/librarian/x", "--commit", candidate, "--bundle", "/x"),
                 ("audit-sign-off", ticket, "--text", "x"), ("mark-done", ticket, "--commit-hash", candidate)):
        answer = run("ticket-board-write", *args)
        check(answer.returncode != 0 and "the Librarian may use only documentation-sign-off" in answer.stderr,
              f"the CLI offers the Librarian nothing but its decisions: {args[0]}: {answer.stderr[-200:]}")
    comment = run("ticket-board-write", "add-comment", ticket, "--text", "Reviewing the help text now.")
    check(comment.returncode == 0, f"but it may comment: {comment.stderr[-200:]}")

    # Audit's finding: a commit that changes code never leaves the Librarian's clone (the board refuses it too).
    (work / "tool.py").write_text("print('the Librarian changed the code')\n")
    git(work, "commit", "-q", "-am", "not documentation")
    wide = run("switchyard-publish-candidate", ticket, "--commit", "HEAD", "--board-url", board.base)
    check(wide.returncode != 0 and "changes more than documentation: tool.py (not a documentation file)" in wide.stderr
          and "Nothing was pushed" in wide.stderr and not git(remote, "for-each-ref", "--format=%(refname)",
                                                              f"refs/heads/{candidate_ref('librarian', ticket, candidate)}"),
          f"the publisher refuses a commit that changes code, and pushes nothing: {wide.stderr[-300:]}")
    git(work, "reset", "-q", "--hard", candidate)
    # Nor does an empty one: nothing edited is documentation-no-change, with its reason.
    git(work, "commit", "-q", "--allow-empty", "-m", "nothing")
    hollow = run("switchyard-publish-candidate", ticket, "--commit", "HEAD", "--board-url", board.base)
    check(hollow.returncode != 0 and "changes nothing against the candidate" in hollow.stderr
          and "documentation-no-change with a reason" in hollow.stderr and "Nothing was pushed" in hollow.stderr
          and not git(remote, "for-each-ref", "--format=%(refname)",
                      f"refs/heads/{candidate_ref('librarian', ticket, candidate)}"),
          f"the publisher refuses an empty commit, and pushes nothing: {hollow.stderr[-300:]}")
    git(work, "reset", "-q", "--hard", candidate)

    (work / "docs").mkdir()
    (work / "docs" / "tool.md").write_text("tool.py prints the new flag.\n")
    git(work, "add", "docs/tool.md")
    git(work, "commit", "-q", "-m", "docs for the new flag")
    doc_commit = git(work, "rev-parse", "HEAD")
    expected = candidate_ref("librarian", ticket, candidate)
    for extra, words in ((["--ref", "roles/librarian/elsewhere"], "chooses no --ref"), (["--replace"], "replaces nothing"),
                         (["--remote", "upstream"], "chooses no --ref or --remote")):
        answer = run("switchyard-publish-candidate", ticket, "--commit", "HEAD", *extra)
        check(answer.returncode != 0 and words in answer.stderr, f"the publisher refuses the Librarian {extra}: "
                                                                 f"{answer.stderr[-200:]}")
    answer = run("switchyard-publish-candidate", "--commit", "HEAD")
    check(answer.returncode != 0 and "publishes for one ticket" in answer.stderr, "and a publication for no ticket")
    printed = run("switchyard-publish-candidate", ticket, "--commit", "HEAD", "--print-only")
    check(printed.returncode == 0 and json.loads(printed.stdout)["ref"] == expected,
          f"it chooses the ticket's documentation ref: {printed.stdout} {printed.stderr[-200:]}")
    implementer = run("switchyard-publish-candidate", ticket, "--commit", "HEAD", "--print-only", caller="app")
    check(json.loads(implementer.stdout)["ref"].startswith("roles/app/"), "other roles publish as before")
    published = run("switchyard-publish-candidate", ticket, "--commit", "HEAD", "--board-url", board.base)
    check(published.returncode == 0 and f"documentation-sign-off {ticket} --commit {doc_commit} --ref {expected}"
          in published.stdout, f"the Librarian publishes there, told how to sign off: {published.stdout[-400:]} "
                               f"{published.stderr[-400:]}")
    check(git(remote, "rev-parse", f"refs/heads/{expected}") == doc_commit and
          git(remote, "rev-parse", "refs/heads/main") != doc_commit, "the remote has it there, and main is untouched")
    signed = run("ticket-board-write", "documentation-sign-off", ticket, "--commit", doc_commit)
    check(signed.returncode == 0 and where(ticket) == f"audit/audit/{doc_commit}",
          f"the real CLI signs off, deriving the same ref, and Audit reviews the documentation commit: "
          f"{signed.stderr[-400:]} {where(ticket)}")
    record = t.psql(board.admin, f"SELECT who || '|' || text FROM ticket_board.ticket_comments WHERE ticket_id = "
                                 f"'{ticket}' ORDER BY id DESC LIMIT 1;")
    check(record.startswith("librarian|Documentation commit") and expected in record and "docs/tool.md" in record,
          f"recorded with both SHAs, the ref and the path: {record}")

    # A merge onto the candidate never leaves the Librarian's clone.
    ticket = "PGU-952"
    git(work, "checkout", "-q", "-b", "side", candidate)
    (work / "other.md").write_text("x\n")
    git(work, "add", "other.md")
    git(work, "commit", "-q", "-m", "side")
    git(work, "checkout", "-q", "--detach", doc_commit)
    git(work, "merge", "-q", "--no-ff", "-m", "a merge", "side")
    merged = run("switchyard-publish-candidate", ticket, "--commit", "HEAD", "--print-only")
    check(merged.returncode != 0 and "has 2 parents" in merged.stderr, f"the publisher refuses a merge: {merged.stderr[-200:]}")


def test_what_a_documentation_commit_may_change() -> None:
    """documentation_gate.documentation_surface and outside_documentation, the policy the board and publisher share."""
    surfaces = {"README.md": True, "docs/guide.md": True, "docs/onboarding/x.rst": True, "docs/img/flow.svg": True,
                "docs/notes.txt": True, "skills/switchyard-librarian/SKILL.md": True, "scripts/ticket_board/README.md": True,
                "CHANGELOG.adoc": True, "scripts/tool.py": False, "requirements.txt": False, "src/notes.txt": False,
                "examples/workflows/inspection.json": False, "tests/fixtures/screen.md": False, "tests/README.md": False,
                "docs/fixtures/golden.md": False, "docs/build.py": False, "src/img/logo.png": False, "../x.md": False,
                "docs//x.md": False, "": False}
    for path, allowed in surfaces.items():
        check(gate.documentation_surface(path) is allowed, f"{path!r} is {'' if allowed else 'not '}documentation")
    check(gate.outside_documentation([("100644", "100644", "M", "docs/a.md"), ("000000", "100644", "A", "docs/b.md"),
                                      ("100644", "000000", "D", "docs/old.md")]) == [],
          "editing, adding and removing documents is a documentation change")
    check(gate.outside_documentation([("100644", "100755", "M", "docs/a.md"), ("000000", "120000", "A", "docs/b.md"),
                                      ("000000", "160000", "A", "docs/c.md"), ("100644", "100644", "M", "a.py")]) == [
        "docs/a.md (mode 100644 -> 100755; only ordinary, non-executable files)",
        "docs/b.md (mode 000000 -> 120000; only ordinary, non-executable files)",
        "docs/c.md (mode 000000 -> 160000; only ordinary, non-executable files)", "a.py (not a documentation file)"],
        "an executable bit, a link, a submodule or a code file is not")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    with temporary_cluster(prefix="syrd624c-", shutdown="immediate") as cluster:
        for db, migrate in (("schema_copy", False), ("migration_copy", True)):
            board = Board(cluster, db, migrate=migrate)
            try:
                exercise_sql_validator(board, db)
            finally:
                board.close()
        board = Board(cluster, "command", migrate=True)
        try:
            exercise_command(cluster, board)
            exercise_librarian_path(cluster, board)
        finally:
            board.close()
    print(f"documentation_gate_command_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
