#!/usr/bin/env python3
"""SYRD-599 (User UAT): Router is a default-off choice when a project is created.

* **Asked:** the real guided role plan offers Router as a labelled menu, default
  off, after two or more implementers, and walks Router's runtime, model and
  effort like any role's; with one implementer it is not offered, and says why.
* **Recorded:** the design artifact carries `project.router` only when Router is
  on; an artifact without it, or with `{"enabled": false}`, is off; malformed
  or impossible records are refused. The real `team-launcher design` takes
  `--router-runtime/--router-model/--router-effort`, off unless given.
* **Applied:** `new_project_command`, from the artifact, declares the audited
  `with_router` document (composed from the plan, or the operator's own),
  projects Router detached with its own model and effort, and leaves the
  artifact loadable for a retry. Off, nothing about Router exists anywhere.
* **The same choice again:** a second run from the artifact and root's
  workflow record both reproduce the same board SQL; an upgrade composes no
  Router.
* **A real board** built from the onboarding SQL holds the active Router, and
  the Director's route and Router's `router-assign` work on it; the board built
  from the off plan declares nothing.

Nothing is provisioned: `new_project_command` runs without --execute against a
recorder, every path is a temporary one, and the boards live in a throwaway
cluster.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

import router_role_command_test as rc  # noqa: E402  (sets sys.path, isolates tmux, provides Board)
from team_launcher_test_helpers import FakeRunner  # noqa: E402

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board import legacy_workflow  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    project_workflow_stages, project_workflow_transitions, workflow_record_document)
from scripts.ticket_board.provision_workflow_sql import render_workflow_sql  # noqa: E402
from scripts.ticket_board.router_setup import ROUTER_PROMPT  # noqa: E402

ROOT = rc.ROOT
t = rc.t
CHECKS = 0
ME = launcher.current_user_name()
ROUTER = (("runtime", "claude"), ("model", "claude-opus-5"), ("effort", "low"))


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def role(doc: dict, name: str) -> dict | None:
    return next((r for r in doc.get("roles", []) if r["name"] == name), None)


# -- asked ----------------------------------------------------------------------------------------------------------

def guided(answers: dict[str, str], *, implementers: str = "") -> tuple[tuple, list[str], str]:
    """The real role plan, answering each prompt by its title; Enter for anything not named."""
    prompts: list[str] = []
    out: list[str] = []

    def input_func(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith("Implementer roles"):
            return implementers
        for title, answer in answers.items():
            if prompt.startswith(title):
                return answer
        return ""

    plan = launcher._prompt_switchyard_role_plan(owner_user="", input_func=input_func, print_func=out.append)
    return plan, prompts, "\n".join(out)


def test_the_guided_plan_offers_router_off_by_default() -> None:
    plan, prompts, out = guided({"Include designer role": "n", "Include audit role": "n"})
    check([p.role for p in plan] == ["director", "main", "ops"],
          f"Enter at the Router menu creates no Router: {[p.role for p in plan]}")
    # SYRD-624: the documentation gate's menu now follows it, as the role plan's last question.
    check(prompts[-2:] == ["Router (optional) [off]: ", "Documentation gate (optional) [off]: "]
          and sum(p.startswith("Router (optional)") for p in prompts) == 1,
          f"asked once, after every role and right before the documentation gate, and off is the default: {prompts[-3:]}")
    check("additional cost" in out and "low-cost model" in out and "1) off -- the Director assigns implementation work"
          in out and "2) on -- Router assigns implementation work" in out,
          f"the menu says what Router does and what it costs, in the selectable style: {out[-700:]}")


def test_turning_router_on_walks_its_runtime_model_and_effort() -> None:
    plan, prompts, _ = guided({"Include designer role": "n", "Include audit role": "n", "Router (optional)": "on",
                               "router runtime": "codex", "router model": "gpt-5.5"})
    router = plan[-1]
    check((router.role, router.cli, router.model, router.effort) == ("router", "codex", "gpt-5.5", "low"),
          f"Router is chosen like any role, effort offered low first: {router}")
    # SYRD-624: then the documentation gate's menu, the role plan's last question.
    check([p.split(" [")[0] for p in prompts[-5:]] == ["Router (optional)", "router runtime", "router model",
                                                     "router effort", "Documentation gate (optional)"],
          f"runtime, then model, then effort, after the choice: {prompts[-5:]}")
    roles, record = launcher_split(plan)
    check([p.role for p in roles] == ["director", "main", "ops"] and record == (("runtime", "codex"),
          ("model", "gpt-5.5"), ("effort", "low")), f"Router is not a role to create, it is the record: {record}")
    from scripts import router_onboarding as ro
    check(ro.summary_line(record) == "switchyard: Router: on (codex -> gpt-5.5 -> effort low) -- assigns implementation "
          "work as one more model session; it starts detached with the other roles"
          and ro.summary_line(()).startswith("switchyard: Router: off -- the Director assigns"),
          "the summary before anything is created names the choice either way")
    pairs = launcher._prompt_switchyard_role_choices(input_func=lambda p: "on" if p.startswith("Router") else "",
                                                     print_func=lambda _l: None)
    check("router" not in dict(pairs), f"the role/runtime pairs never include Router: {pairs}")


def launcher_split(plan: tuple) -> tuple:
    from scripts import router_onboarding as ro
    return ro.split_role_plan(plan)


def test_one_implementer_is_not_offered_router() -> None:
    plan, prompts, out = guided({"Include designer role": "n", "Include audit role": "n"}, implementers="2")
    check([p.role for p in plan] == ["director", "ops"], f"one implementer: {[p.role for p in plan]}")
    check(not any(p.startswith("Router") for p in prompts)
          and "Router is not offered: with one implementer (ops) every ticket already has its owner" in out,
          f"the menu is not shown, and the operator is told why: {out[-300:]}")


# -- recorded -------------------------------------------------------------------------------------------------------

def write_artifact(path: Path, repo: Path, *, router=(), roles=("main", "ops"), full: bool = False,
                   role_models: dict | None = None) -> None:
    fixed = (("designer", "claude"), ("director", "claude"), ("audit", "claude")) if full else (("director", "claude"),)
    launcher._write_initial_switchyard_project_artifact(
        project_name="Cerulean", slug="cerulean", owner_user=ME, project_dir=repo, artifact_path=path,
        design_document=repo / "DESIGN.md", owner_shell="/bin/bash", implementer_roles=roles,
        role_clis=(*fixed, *((r, "codex") for r in roles)), include_designer=full,
        include_audit=full, audit_roles=("audit",) if full else (), router=router, role_models=role_models)


def refused(path: Path, record: object) -> str:
    raw = json.loads(path.read_text())
    raw["project"]["router"] = record
    bad = path.with_name("bad.project.json")
    bad.write_text(json.dumps(raw))
    try:
        launcher.load_project_design_artifact(bad)
    except SystemExit as exc:
        return str(exc)
    return "loaded"


def test_the_design_record() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd599o-") as tmp:
        tmp_path = Path(tmp)
        on, off = tmp_path / "on" / "cerulean.project.json", tmp_path / "off" / "cerulean.project.json"
        write_artifact(on, tmp_path, router=ROUTER)
        write_artifact(off, tmp_path)
        check(json.loads(on.read_text())["project"]["router"] == {"enabled": True, "runtime": "claude",
                                                                 "model": "claude-opus-5", "effort": "low"},
              "on is written as an explicit record")
        check("router" not in json.loads(off.read_text())["project"], "off writes nothing, as before")
        check(launcher.load_project_design_artifact(on).router == ROUTER
              and launcher.load_project_design_artifact(off).router == (), "both read back as they were chosen")
        legacy = json.loads(off.read_text())
        legacy["project"].pop("role_models", None)
        old = tmp_path / "old.project.json"
        old.write_text(json.dumps(legacy))
        check(launcher.load_project_design_artifact(old).router == (), "an artifact from before the field is off")
        disabled = json.loads(on.read_text())
        disabled["project"]["router"] = {"enabled": False, "runtime": "claude"}
        explicit = tmp_path / "explicit-off.project.json"
        explicit.write_text(json.dumps(disabled))
        check(launcher.load_project_design_artifact(explicit).router == (), "an explicit {enabled: false} is off")
        for record, words in (
            ("yes", "must be a JSON object"),
            ({"enabled": "true"}, "project.router.enabled' must be a JSON boolean"),
            ({"enabled": True}, "runtime must be one of"),
            ({"enabled": True, "runtime": "vim"}, "runtime must be one of"),
            ({"enabled": True, "runtime": "claude", "slot": 1}, "unknown key 'slot'"),
            ({"enabled": True, "runtime": "agy", "effort": "low"}, "gives Router an effort, which agy does not take"),
            ({"enabled": True, "runtime": "claude", "model": 5}, "project.router.model' must be a JSON string"),
        ):
            said = refused(on, record)
            check(words in said, f"{record!r} is refused: {said}")
        single = tmp_path / "single" / "cerulean.project.json"
        write_artifact(single, tmp_path, roles=("main",))
        said = refused(single, {"enabled": True, "runtime": "claude"})
        check("enables Router with one implementer (main)" in said, f"one implementer cannot enable it: {said}")
        named = tmp_path / "named" / "cerulean.project.json"
        write_artifact(named, tmp_path, roles=("main", "router"))
        said = refused(named, {"enabled": True, "runtime": "claude"})
        check("names an implementer 'router'" in said, f"an implementer called router cannot coexist: {said}")


def design(tmp_path: Path, *extra: str) -> tuple[object, dict, str]:
    """The real `team-launcher design`, every other answer given by flag."""
    target = tmp_path / f"design-{len(list(tmp_path.glob('design-*')))}"
    argv = ["cerulean", "design", "--design-output-dir", str(target), "--design-title", "Cerulean",
            "--design-body", "b", "--repository", str(tmp_path), "--remote", "origin", "--default-branch", "main",
            "--worktree-policy", "shared", "--owner-user", ME, "--ticket-prefix", "CER", "--push-policy", "p",
            "--audit-signoff", "--no-needs-inspection", "--no-needs-user-signoff", "--board-service-traversal",
            "--supplementary-group", "users", "--linger", "--owner-shell", "/bin/bash", *extra]
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        try:
            code = launcher.main(argv)
        except SystemExit as exc:
            code = exc.code
    files = list(target.glob("*.project.json"))
    return code, (json.loads(files[0].read_text())["project"] if files else {}), err.getvalue()


def test_the_noninteractive_design_command() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd599d-") as tmp:
        tmp_path = Path(tmp)
        code, project, _ = design(tmp_path)
        check(code == 0 and "router" not in project, f"no flag, no Router: {code} {project.get('router')}")
        code, project, _ = design(tmp_path, "--router-runtime", "codex", "--router-model", "gpt-5.5",
                                  "--router-effort", "low")
        check(code == 0 and project.get("router") == {"enabled": True, "runtime": "codex", "model": "gpt-5.5",
                                                      "effort": "low"}, f"the flags record it: {project.get('router')}")
        for extra, words in ((("--router-model", "gpt-5.5"), "need --router-runtime"),
                             (("--router-runtime", "vim"), "runtime must be one of"),
                             (("--router-runtime", "agy", "--router-effort", "low"), "does not take")):
            code, project, _ = design(tmp_path, *extra)
            check(code not in (0, None) and words in str(code) and not project,
                  f"{extra} is refused before anything is written: {code}")


# -- applied --------------------------------------------------------------------------------------------------------

def provision(artifact: Path, out: Path, **extra: object) -> tuple[int, str]:
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        try:
            code = launcher.new_project_command("cerulean", from_artifact=artifact, source_repo=ROOT, output_dir=out,
                                                runner=FakeRunner(), port_in_use=lambda _p: False,
                                                socket_exists=lambda _p: False, port=24599,
                                                database="cerulean_board", **extra)
        except SystemExit as exc:  # a refusal is judged by the checks, not a crash
            code, _ = exc.code, printed.write(f"\nrefused: {exc.code}")
    return code, printed.getvalue()


def test_provisioning_from_the_record(state: dict) -> None:
    tmp_path = state["tmp"]
    on_artifact = tmp_path / "on" / ".switchyard" / "cerulean.project.json"
    off_artifact = tmp_path / "off" / ".switchyard" / "cerulean.project.json"
    write_artifact(on_artifact, tmp_path / "on", router=ROUTER)
    write_artifact(off_artifact, tmp_path / "off")
    code, printed = provision(on_artifact, tmp_path / "out-on")
    check(code == 0 and "starts with a declared workflow, the one its plan describes plus Router" in printed,
          f"a Router project is provisioned and says how: {code} {printed[-400:]}")
    plan = json.loads((tmp_path / "out-on" / "plan.json").read_text())
    router = role(plan["workflow"], "router")
    check(router is not None and router["active"] and router["kind"] == "router" and router["runtime"] == "claude"
          and router["slot"] is None and router["target"] == "cerulean-router:0.0"
          and router["onboarding_prompt"] == ROUTER_PROMPT and router["capabilities"] == ["router_assign"],
          f"the plan declares the audited Router: {router}")
    actions = {x["action"] for x in plan["workflow"]["transitions"]}
    check({"router_return", "implementer_kick_back"} <= actions, f"with its return and the kick-back: {actions}")
    sql = (tmp_path / "out-on" / "cerulean-workflow.sql").read_text()
    check("ticket_board.apply_declared_workflow(" in sql and '"router"' in sql, "the board SQL declares it")
    config = json.loads((tmp_path / "out-on" / "cerulean.json").read_text())
    entry = next((r for r in config["roles"] if r["role"] == "router"), None)
    check(entry is not None and entry.get("detached") is True and "slot" not in entry and entry["cli"] == ["claude"]
          and entry.get("model") == "claude-opus-5" and entry.get("effort") == "low"
          and entry["env"].get("TICKET_BOARD_ROLE_ONBOARDING_PROMPT") == ROUTER_PROMPT,
          f"the launcher starts Router detached, on its own model and effort, not its template's: {entry}")
    check([r["role"] for r in config["roles"] if not r.get("detached")] == ["director", "main", "ops"]
          and {r["role"]: r.get("slot") for r in config["roles"] if not r.get("detached")}
          == {"director": 0, "main": 1, "ops": 2}, "every other pane keeps its place")
    loaded = launcher.load_project_config("cerulean", tmp_path / "out-on" / "cerulean.json")
    check(any(r.role == "router" and r.detached for r in loaded.roles), "the launcher loads it as a detached role")
    reread = json.loads(on_artifact.read_text())
    check("workflow" not in reread and launcher.load_project_design_artifact(on_artifact).router == ROUTER,
          "the artifact still describes the choice and still loads, so a retry can use it")
    state.update(on_sql=sql, on_plan=plan, out_on=tmp_path / "out-on", on_artifact=on_artifact)

    code, printed = provision(off_artifact, tmp_path / "out-off")
    check(code == 0 and "Router" not in printed, f"off says nothing of Router: {printed[-300:]}")
    off_plan = json.loads((tmp_path / "out-off" / "plan.json").read_text())
    off_sql = (tmp_path / "out-off" / "cerulean-workflow.sql").read_text()
    off_config = (tmp_path / "out-off" / "cerulean.json").read_text()
    check(off_plan.get("workflow") is None and "apply_declared_workflow(" not in off_sql
          and "router" not in off_sql.lower() and "router" not in off_config
          and not (tmp_path / "out-off" / "workflow.json").exists(),
          "off: no declared workflow, and nothing named Router in the SQL, the plan or the launcher config")
    state.update(off_sql=off_sql)

    full_artifact = tmp_path / "full" / ".switchyard" / "cerulean.project.json"
    write_artifact(full_artifact, tmp_path / "full", router=ROUTER, full=True)
    code, _ = provision(full_artifact, tmp_path / "out-full")
    full_plan = json.loads((tmp_path / "out-full" / "plan.json").read_text())
    check(code == 0 and {"designer", "audit", "router"} <= {r["name"] for r in full_plan["workflow"]["roles"]}
          and "inspection" in full_plan["workflow"]["remove_stages"],
          f"the default team with Router: designer and audit kept, the unused seeded stage named for removal: {code}")
    state.update(full_sql=(tmp_path / "out-full" / "cerulean-workflow.sql").read_text())

    bare_artifact = tmp_path / "bare" / ".switchyard" / "cerulean.project.json"
    write_artifact(bare_artifact, tmp_path / "bare", router=(("runtime", "claude"),), role_models={"main": "gpt-5.5"})
    code, _ = provision(bare_artifact, tmp_path / "out-bare")
    bare = json.loads((tmp_path / "out-bare" / "cerulean.json").read_text()) if code == 0 else {"roles": []}
    entry = next((r for r in bare["roles"] if r["role"] == "router"), {})
    template = next((r for r in bare["roles"] if r["role"] == "main"), {})
    check(code == 0 and entry.get("cli") == ["claude"] and "model" not in entry and "effort" not in entry
          and template.get("model") == "gpt-5.5",
          f"Router with no model chosen runs its runtime's default, never its template's model: {entry}")


def test_a_retry_and_roots_record_reuse_the_choice(state: dict) -> None:
    tmp_path = state["tmp"]
    code, _ = provision(state["on_artifact"], tmp_path / "out-retry")
    check(code == 0 and (tmp_path / "out-retry" / "cerulean-workflow.sql").read_text() == state["on_sql"],
          "running again from the artifact renders the same board SQL")
    record = json.loads((state["out_on"] / "workflow.json").read_text())
    document, problem = workflow_record_document(record, project="cerulean")
    check(document is not None and role(document, "router")["active"], f"root's record carries Router: {problem}")
    base = launcher.build_plan(project="cerulean", owner_user=ME, implementer_roles=("main", "ops"),
                               include_designer=False, include_audit=False, audit_roles=(), port=24599,
                               database="cerulean_board", ticket_prefix="CERULEAN")
    check(render_workflow_sql(dataclasses.replace(base, workflow=document)) == state["on_sql"],
          "and resume-provision, rebuilding from that record, renders the same SQL")
    composed = legacy_workflow.compose_legacy_workflow(
        base, canonical=legacy_workflow.load_canonical(ROOT), stage_seeds=project_workflow_stages(base),
        transition_seeds=project_workflow_transitions(base),
        panes={"director": {"runtime": "claude", "target": "cerulean-director:0.0", "slot": 0}})
    check(role(composed.document, "router") is None, "migrate-workflow, as an upgrade composes it, adds no Router")
    for module in ("project_upgrade_command", "upgrade_phases", "director_upgrade", "resume_provision_command",
                   "root_plan_reconstruction", "workflow_adoption"):
        text = (ROOT / "scripts" / f"{module}.py").read_text()
        check("router_onboarding" not in text and "load_project_design_artifact" not in text,
              f"{module} never reads the design artifact, so no upgrade or resume can turn Router on")


def test_an_operators_own_workflow_gains_router(state: dict) -> None:
    tmp_path = state["tmp"]
    own = rc.validate(copy.deepcopy(rc.SHIPPED))
    own["project"] = "cerulean"
    config = tmp_path / "own-workflow.json"
    config.write_text(json.dumps(own))
    code, printed = provision(state["on_artifact"], tmp_path / "out-own", workflow_config=config)
    plan = json.loads((tmp_path / "out-own" / "plan.json").read_text())
    check(code == 0 and role(plan["workflow"], "router")["active"]
          and {s["name"] for s in plan["workflow"]["stages"]} == {s["name"] for s in own["stages"]}
          and "the one its plan describes" not in printed,
          f"--workflow-config is kept, and Router is added to it rather than composed: {code}")
    # Audit, round 3: the operator-workflow path must be retryable from its artifact too.
    artifact = json.loads(state["on_artifact"].read_text())
    again, said = provision(state["on_artifact"], tmp_path / "out-own-retry", workflow_config=config)
    first = (tmp_path / "out-own" / "cerulean-workflow.sql").read_text()
    retried = (tmp_path / "out-own-retry" / "cerulean-workflow.sql").read_text() if again == 0 else ""
    check("workflow" not in artifact and again == 0 and retried == first,
          f"and a retry from the same artifact and --workflow-config renders the same board SQL: {again} {said[-300:]}")


# -- a real board ---------------------------------------------------------------------------------------------------

def exercise_boards(cluster, state: dict) -> None:
    def apply(board: rc.Board, sql: str) -> str:
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", board.admin], input=sql,
                              capture_output=True, text=True, timeout=120)
        return "" if done.returncode == 0 else done.stderr[-400:]

    board = rc.Board(cluster, "onboarded", migrate=True)
    try:
        said = apply(board, state["on_sql"])
        check(said == "", f"a fresh board takes the onboarding SQL, as the provisioning packet runs it: {said}")
        stored = board.document()
        router = role(stored, "router")
        check(router is not None and router["active"] and router["runtime"] == "claude",
              "the board created from the onboarding SQL runs an active Router")
        rc.t.TEST_WRITE_TOKEN = board.server.write_token
        from scripts.ticket_board.peer_identity import read_process
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "TICKET_BOARD_URL": board.base,
               "TICKET_BOARD_WRITE_TOKEN": board.server.write_token}

        def cli(*args: str, caller: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([str(ROOT / "scripts" / args[0]), *args[1:]], capture_output=True, text=True,
                                  timeout=60, env={**env, "TICKET_BOARD_CALLER_ROLE": caller}, stdin=subprocess.DEVNULL)

        def where(ticket: str) -> str:
            return t.psql(board.admin, f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{ticket}';")

        t.seed_postgres_ticket(board.admin, "PGU-951", title="first", state="analysis", assignee="director",
                               commit_exempt=True)
        sleeper = subprocess.Popen(["sleep", "3600"])
        try:
            t.psql(board.admin, "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, "
                                "worktree, session_dir, process_pid, process_start_time, process_uid) VALUES ('router', "
                                f"'claude', 'cerulean-router:0.0', '/w/router', '/s/router', {sleeper.pid}, "
                                f"{read_process(sleeper.pid).start_time}, {os.getuid()});")
            routed = cli("ticket-board-write", "route", "PGU-951", "--state", "in_progress", caller="director")
            check(routed.returncode == 0 and where("PGU-951") == "in_progress/router",
                  f"the Director's plain route goes to Router: {routed.stderr[-300:]} {where('PGU-951')}")
            revision = json.loads(cli("ticket-board-read", "ticket", "PGU-951", "--json", caller="router").stdout)[
                "routing"]["revision"]
            done = cli("ticket-board-write", "router-assign", "PGU-951", "--assignee", "ops", "--revision", revision,
                       caller="router")
            check(done.returncode == 0 and where("PGU-951") == "in_progress/ops",
                  f"and Router assigns it: {done.stderr[-300:]} {where('PGU-951')}")
        finally:
            sleeper.kill()
            sleeper.wait()
    finally:
        board.close()
    board = rc.Board(cluster, "onboarded_full", migrate=True)
    try:
        said = apply(board, state["full_sql"])
        check(said == "", f"and the default team's: {said}")
        stored = board.document()
        check(role(stored, "router")["active"] and role(stored, "audit")["active"] and role(stored, "designer")["active"]
              and "inspection" not in {s["name"] for s in stored["stages"]},
              "a fresh board takes the default team's Router workflow too")
    finally:
        board.close()
    board = rc.Board(cluster, "onboarded_off", migrate=True)
    try:
        said = apply(board, state["off_sql"])
        check(said == "", f"and the off plan's: {said}")
        declared = t.psql(board.admin, "SELECT count(*) FROM ticket_board.workflow_configuration WHERE document IS NOT NULL;")
        check(declared == "0", f"the board created with Router off declares nothing: {declared}")
    finally:
        board.close()


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and fn.__code__.co_argcount == 0:
            fn()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    with tempfile.TemporaryDirectory(prefix="syrd599p-") as tmp:
        state: dict = {"tmp": Path(tmp)}
        test_provisioning_from_the_record(state)
        test_a_retry_and_roots_record_reuse_the_choice(state)
        test_an_operators_own_workflow_gains_router(state)
        with rc.temporary_cluster(prefix="syrd599o-", shutdown="immediate") as cluster:
            exercise_boards(cluster, state)
    print(f"router_onboarding_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
