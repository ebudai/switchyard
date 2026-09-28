#!/usr/bin/env python3
"""SYRD-370: `switchyard new`'s host-and-CLI phase, against the command it came out of.

The nine statements from `stages.begin("host and agent CLI checks")` through
the agent-CLI gate moved into `_check_new_project_preflight` in
`scripts/new_project_phases.py`, which returns a frozen `NewProjectPreflight`;
the command calls it where they were and reads back the four values the rest
of it uses. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first.
- **Seams (rule 24).** Every launcher facility the phase uses is read from the
  launcher when it runs, as often as the command read it; nothing it binds is.
- **The behaviour is unchanged:** the stage begun first, the project path
  checked before anything else, the source checkout and the design artifact,
  every field of the provisioning plan, the precheck with the plain runner, the
  agy validation only when there is a source, and the CLI gate last -- whose
  answer replaces P0's roles.
- **The caller.** The command hands the phase P0's very objects and its own
  values, reads back each field as the phase returned it, and goes on to P2.

Every facility is this test's own fake: each launcher seam, the stages, the
design artifact and the plan. Nothing is looked up, provisioned or run; the
only paths are inside temporary directories this test owns, and the command is
stopped by a fake at the start of P2, before anything there could act.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as t  # noqa: E402
from scripts import new_project_phases as m  # noqa: E402

CHECKS = 0
#: Measured on the baseline command's nine P1 statements.
SEAMS = {"_parse_agent_cli_sources": 1, "_precheck_project_path_before_mutating": 1, "_repo_root": 1,
         "_validate_agy_credential_source": 1, "build_plan": 1, "load_project_design_artifact": 1,
         "precheck_new_project": 1, "require_agent_clis_for_new_tenant": 1}
PARAMETERS = ("from_artifact", "source_repo", "commit_git_dir", "port", "database", "yes", "home_base", "port_in_use",
              "socket_exists", "config_dir", "registry_dir", "input_func", "print_func", "agent_cli_policy",
              "agent_cli_sources")
FROM_P0 = ("include_audit", "include_designer", "owner_user", "project_dir", "resolved_agy_credential_source",
           "resolved_project_name", "resolved_slug", "runner", "selected_audit_roles", "selected_implementer_roles",
           "selected_role_clis", "stages")
OUTPUTS = ("effective_source_repo", "precheck_plan", "selected_role_clis", "worktree_branch")
OWNER = "syrd370-agent"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def judged(function, *args: object, **kwargs: object) -> object:
    """What a call returned, or what it raised, as a value to compare."""
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- SystemExit is an answer here, and so is anything a mutant raises
        return exc


class patched:
    """Rebind attributes of one object for one block, as the suites do."""

    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values = target, values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.target, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.target, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


class Stages:
    def __init__(self, log: list) -> None:
        self.log = log

    def begin(self, name: str, **kwargs: object) -> None:
        self.log.append(("begin", name))


class World:
    """One run's fakes, recording in order; `fail` names the step that refuses."""

    def __init__(self, tmp: Path, *, fail: str | None = None, artifact: object = None) -> None:
        self.tmp, self.fail, self.artifact, self.log = tmp, fail, artifact, []
        self.gated = (("director", "claude"), ("main", "codex-promoted"))
        self.parsed = {"codex": "/fixture/codex"}

    def step(self, name: str, *detail: object) -> None:
        self.log.append((name, *detail))
        if self.fail == name:
            raise SystemExit(f"refused at {name}")

    def seams(self) -> dict:
        L = self

        def gate(clis, **kw):
            L.step("gate", clis, kw)
            return L.gated

        return dict(
            _precheck_project_path_before_mutating=lambda owner, project_dir: L.step("path", owner, project_dir),
            _repo_root=lambda: L.step("repo-root") or L.tmp / "repo-root",
            load_project_design_artifact=lambda path, *, expected_project: L.step("artifact", path, expected_project) or L.artifact,
            build_plan=lambda **kw: L.step("plan", kw) or SimpleNamespace(service_user=f"{kw['project']}-board", **kw),
            precheck_new_project=lambda plan, **kw: L.step("precheck", plan, kw),
            _validate_agy_credential_source=lambda source, owner, home: L.step("agy", source, owner, home),
            _parse_agent_cli_sources=lambda values: L.step("sources", values) or L.parsed,
            require_agent_clis_for_new_tenant=gate,
        )

    def names(self) -> list:
        return [entry[0] for entry in self.log]


def inputs(tmp: Path, world: World, **overrides: object) -> dict:
    values = dict(from_artifact=None, source_repo=tmp / "a" / ".." / "source", commit_git_dir=".git-fixture", port=15432,
                  database="syrd370_db", yes=False, home_base=tmp / "homes", port_in_use=lambda port: False,
                  socket_exists=lambda path: False, config_dir=tmp / "config", registry_dir=tmp / "registry",
                  input_func=refuse("the terminal"), print_func=lambda line: None, agent_cli_policy="ask",
                  agent_cli_sources=("codex=/fixture/codex",), include_audit=True, include_designer=False,
                  owner_user=OWNER, project_dir=tmp / "project", resolved_agy_credential_source="",
                  resolved_project_name="Syrd Three Seventy", resolved_slug="s370", runner=lambda *a, **k: None,
                  selected_audit_roles=("audit",), selected_implementer_roles=("main",),
                  selected_role_clis=(("director", "claude"), ("main", "codex")), stages=Stages(world.log))
    values.update(overrides)
    return values


def run(world: World, values: dict) -> object:
    with patched(t, **world.seams()):
        return judged(m._check_new_project_preflight, **values)


def design(**fields: object) -> SimpleNamespace:
    base = dict(default_branch="trunk", project_name="Designed", ticket_prefix="DSG", implementer_roles=("builder",),
                include_designer=True, include_audit=False, audit_roles=("audit", "audit-2"),
                capability_grants={"board_service_traversal": 0})
    base.update(fields)
    return SimpleNamespace(**base)


def module_tree() -> ast.Module:
    return ast.parse((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"))


def phase_def() -> ast.FunctionDef:
    return next(n for n in module_tree().body if isinstance(n, ast.FunctionDef) and n.name == "_check_new_project_preflight")


#: The six phases and two continuation types `switchyard new` reads through the launcher (SYRD-425).
NEW_COMMAND_READS = ("_resolve_new_project_choices", "_check_new_project_preflight", "_prepare_new_project_accounts",
                     "_prepare_new_project_board", "_run_new_project_sign_in", "_launch_new_project_panes",
                     "NewProjectBoard", "NewProjectSignIn")


def command_def() -> ast.FunctionDef:
    """`switchyard_new_command` as its phases' wiring sees it.

    SYRD-425 moved it to scripts/switchyard_new_command.py, where it reads each phase and continuation type through
    the launcher when it runs. Checked first, on the source as it is: the launcher no longer defines it, re-exports it
    unaliased and still dispatches `new` to that name; its first statement is the call-time launcher import; and every
    phase and continuation is read through the launcher, none bare. Only then is the import dropped and each
    `launcher.X` read as `X`, so the positions and names below are the command's own. Before the move (the baseline)
    it is the launcher's definition as it stands.
    """
    moved = ROOT / "scripts" / "switchyard_new_command.py"
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    tree = ast.parse(moved.read_text(encoding="utf-8")) if moved.exists() else launcher_tree
    command = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "switchyard_new_command")
    if moved.exists():
        check(not any(isinstance(n, ast.FunctionDef) and n.name == "switchyard_new_command" for n in launcher_tree.body),
              "the launcher no longer defines switchyard_new_command")
        check(any(isinstance(n, ast.ImportFrom) and n.module == "scripts.switchyard_new_command"
                  and any(a.name == "switchyard_new_command" and a.asname is None for a in n.names) for n in launcher_tree.body),
              "the launcher re-exports it, unaliased")
        main = next(n for n in launcher_tree.body if isinstance(n, ast.FunctionDef) and n.name == "switchyard_main")
        check(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "switchyard_new_command" for n in ast.walk(main)),
              "and switchyard_main still dispatches `new` to the launcher's name")
        check(ast.unparse(command.body[0]) == "from scripts import team_launcher as launcher",
              f"the command imports the launcher first thing, when it runs: {ast.unparse(command.body[0])}")
        bare = sorted({n.id for n in ast.walk(command) if isinstance(n, ast.Name) and n.id in NEW_COMMAND_READS})
        through = sorted({n.attr for n in ast.walk(command)
                          if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher"})
        check(bare == [] and through == sorted(NEW_COMMAND_READS),
              f"every phase and continuation is read through the launcher, none bare: {bare} {through}")
        del command.body[0]

        class AsLauncherGlobal(ast.NodeTransformer):
            def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
                self.generic_visit(node)
                if isinstance(node.value, ast.Name) and node.value.id == "launcher":
                    return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
                return node

        AsLauncherGlobal().visit(command)
    return command


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.new_project_phases as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.new_project_phases", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.new_project_phases")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.new_project_phases as m; "
                        "print(t._check_new_project_preflight is m._check_new_project_preflight, "
                        "t.NewProjectPreflight is m.NewProjectPreflight, t._resolve_new_project_choices is m._resolve_new_project_choices)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_and_nothing_bound_read_through_the_launcher() -> None:
    fn = phase_def()
    check(ast.unparse(fn.body[0]) == "from scripts import team_launcher as launcher", "the launcher imported first, when it runs")
    through: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "launcher":
            through[node.attr] = through.get(node.attr, 0) + 1
    check(through == SEAMS, f"each launcher name read through it exactly as often as the command read it: {through}")
    written = {id(n) for a in ast.walk(fn) if isinstance(a, ast.arg) and a.annotation is not None for n in ast.walk(a.annotation)}
    bare = sorted({n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in SEAMS and id(n) not in written})
    check(bare == [], f"and none of them read past it: {bare}")
    bound = {a.arg for a in fn.args.kwonlyargs} | {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    check(not bound & set(through), f"nothing the phase binds is read through the launcher: {bound & set(through)}")
    check(ast.unparse(fn.body[1]) == "stages.begin('host and agent CLI checks')", "the stage begun first")
    gate = fn.body[-2]
    check(ast.unparse(gate).startswith("selected_role_clis = launcher.require_agent_clis_for_new_tenant("),
          "and the CLI gate last, right before the answer")


def test_the_interface_and_the_result() -> None:
    fn = phase_def()
    args = fn.args
    check(not args.args and not args.posonlyargs and not args.vararg and not args.kwarg
          and tuple(a.arg for a in args.kwonlyargs) == PARAMETERS + FROM_P0 and all(d is None for d in args.kw_defaults),
          f"keyword-only, the command's parameters then P0's values, no defaults: {[a.arg for a in args.kwonlyargs]}")
    theirs = {a.arg: ast.unparse(a.annotation) for a in command_def().args.kwonlyargs}
    choices = next(n for n in module_tree().body if isinstance(n, ast.ClassDef) and n.name == "NewProjectChoices")
    theirs.update({s.target.id: ast.unparse(s.annotation) for s in choices.body if isinstance(s, ast.AnnAssign)})
    check(all(ast.unparse(a.annotation) == theirs[a.arg] for a in args.kwonlyargs), "each annotated as where it comes from")
    fields = dataclasses.fields(m.NewProjectPreflight)
    check(tuple(f.name for f in fields) == OUTPUTS, f"the four values, in order: {[f.name for f in fields]}")
    check(all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields), "no defaults")
    check(m.NewProjectPreflight.__dataclass_params__.frozen, "frozen")
    check(sum(isinstance(n, ast.Return) for n in ast.walk(fn)) == 1
          and [k.arg for k in fn.body[-1].value.keywords] == list(OUTPUTS)
          and all(isinstance(k.value, ast.Name) and k.value.id == k.arg for k in fn.body[-1].value.keywords),
          "one way out but a refusal: each field from the local of its name")


def test_the_command_calls_it_where_the_checks_were() -> None:
    body = command_def().body
    at = next(i for i, s in enumerate(body) if isinstance(s, ast.Assign) and ast.unparse(s.targets[0]) == "new_project_preflight")
    check(ast.unparse(body[at - 1]) == "stages = new_project_choices.stages", "right after P0's read-backs")
    call = body[at].value
    check(ast.unparse(call.func) == "_check_new_project_preflight" and not call.args
          and [(k.arg, ast.unparse(k.value)) for k in call.keywords] == [(n, n) for n in PARAMETERS + FROM_P0],
          "called by the launcher's name, with the command's own values")
    back = [ast.unparse(s) for s in body[at + 1:at + 1 + len(OUTPUTS)]]
    check(back == [f"{f} = new_project_preflight.{f}" for f in OUTPUTS], f"every field read back to its old name: {back}")
    after = body[at + 1 + len(OUTPUTS)]
    if ast.unparse(after) != "stages.begin('project accounts and files')":
        # SYRD-371: P2 is its own phase now, and that phase begins it first.
        accounts = next(n for n in module_tree().body if isinstance(n, ast.FunctionDef) and n.name == "_prepare_new_project_accounts")
        check(isinstance(after, ast.Assign) and isinstance(after.value, ast.Call)
              and ast.unparse(after.value.func) == "_prepare_new_project_accounts"
              and ast.unparse(accounts.body[1]) == "stages.begin('project accounts and files')",
              f"then the P2 phase, which begins it first: {ast.unparse(after)[:80]}")
    else:
        check(True, "then P2 begins, as before")


# --- behaviour -------------------------------------------------------------------------------------------------------


def test_the_order_without_an_artifact() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        values = inputs(tmp, world, resolved_agy_credential_source="hostuser")
        got = run(world, values)
        check(isinstance(got, m.NewProjectPreflight), f"it goes on: {got!r}")
        check(world.names() == ["begin", "path", "plan", "precheck", "agy", "sources", "gate"], f"the order: {world.names()}")
        check(world.log[0] == ("begin", "host and agent CLI checks") and world.log[1] == ("path", OWNER, tmp / "project"),
              "the stage begun, then the project path checked before anything else")
        check(got.effective_source_repo == (tmp / "source").resolve(strict=False), f"the caller's source, resolved: {got.effective_source_repo}")
        check(got.worktree_branch == "main", "no artifact: main")


def test_the_source_checkout_falls_back_to_this_one() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        got = run(world, inputs(tmp, world, source_repo=None))
        check(world.names()[2] == "repo-root" and got.effective_source_repo == (tmp / "repo-root").resolve(strict=False),
              f"the launcher's own checkout when none is given: {world.names()}")


def test_the_plan_from_the_choices() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        values = inputs(tmp, world)
        got = run(world, values)
        plan = next(e[1] for e in world.log if e[0] == "plan")
        check(plan == dict(project="s370", project_name="Syrd Three Seventy", owner_user=OWNER, owner_home=tmp / "homes" / OWNER,
                           port=15432, database="syrd370_db", source_repo=got.effective_source_repo, commit_git_dir=".git-fixture",
                           ticket_prefix=None, implementer_roles=values["selected_implementer_roles"], include_designer=False,
                           include_audit=True, audit_roles=values["selected_audit_roles"], board_service_traversal=True),
              f"every field of the plan from P0's values: {plan}")
        check(plan["implementer_roles"] is values["selected_implementer_roles"] and plan["audit_roles"] is values["selected_audit_roles"],
              "P0's very objects")
        check(got.precheck_plan == SimpleNamespace(service_user="s370-board", **plan), "and the plan is returned")
        check("artifact" not in world.names(), "no artifact read")


def test_the_plan_from_the_design_artifact() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        world = World(tmp, artifact=design())
        got = run(world, inputs(tmp, world, from_artifact=tmp / "design.json"))
        check(world.log[2] == ("artifact", tmp / "design.json", "s370"), f"the artifact read with the expected project, after the path check: {world.log[:3]}")
        plan = next(e[1] for e in world.log if e[0] == "plan")
        check((plan["project_name"], plan["ticket_prefix"], plan["implementer_roles"], plan["include_designer"], plan["include_audit"],
               plan["audit_roles"]) == ("Designed", "DSG", ("builder",), True, False, ("audit", "audit-2")),
              f"name, prefix, implementers, designer, audit and audit roles from the artifact: {plan}")
        check(plan["project"] == "s370" and plan["owner_user"] == OWNER, "project and owner still P0's")
        check(plan["board_service_traversal"] is False, "a granted traversal of 0 read as False")
        check(got.worktree_branch == "trunk", "the artifact's branch")
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        world = World(tmp, artifact=design(capability_grants={}))
        run(world, inputs(tmp, world, from_artifact=tmp / "design.json"))
        check(next(e[1] for e in world.log if e[0] == "plan")["board_service_traversal"] is True, "an unset traversal is True")


def test_the_precheck() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        values = inputs(tmp, world)
        got = run(world, values)
        plan, kw = next(e[1:] for e in world.log if e[0] == "precheck")
        check(plan is got.precheck_plan, "the plan just built")
        check(kw == dict(source_repo=got.effective_source_repo, repository=tmp / "project", runner=values["runner"],
                         port_in_use=values["port_in_use"], socket_exists=values["socket_exists"], config_dir=tmp / "config",
                         registry_dir=tmp / "registry", require_owner_user=False, require_repository=False),
              f"prechecked as before: {kw}")
        check(kw["runner"] is values["runner"], "with the plain runner P0 returned, not another")


def test_the_agy_source_only_when_there_is_one() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        run(world, inputs(tmp, world, resolved_agy_credential_source="hostuser"))
        check(("agy", "hostuser", OWNER, tmp / "homes") in world.log, "validated for the owner under the home base")
        world = World(tmp)
        run(world, inputs(tmp, world, resolved_agy_credential_source=""))
        check("agy" not in world.names(), "and not at all without a source")


def test_the_cli_gate_last_and_its_answer_kept() -> None:
    for yes in (False, True):
        with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
            tmp = Path(tmp)
            world = World(tmp)
            values = inputs(tmp, world, yes=yes)
            got = run(world, values)
            check(("sources", values["agent_cli_sources"]) in world.log, "the declared sources parsed")
            clis, kw = next(e[1:] for e in world.log if e[0] == "gate")
            check(clis is values["selected_role_clis"] and kw == dict(owner_user=OWNER, policy="ask", sources=world.parsed,
                                                                       interactive=not yes, input_func=values["input_func"],
                                                                       print_func=values["print_func"]),
                  f"P0's roles, for the owner, the policy, the parsed sources, asking only without --yes: {kw}")
            check(kw["sources"] is world.parsed, "the parsed sources themselves")
            check(world.names()[-1] == "gate", "last")
            check(got.selected_role_clis is world.gated, "and its answer replaces P0's roles")


def test_every_refusal_stops_what_follows() -> None:
    expected = ["begin", "path", "artifact", "plan", "precheck", "agy", "sources", "gate"]
    for step in ("path", "artifact", "plan", "precheck", "agy", "sources", "gate"):
        with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
            tmp = Path(tmp)
            world = World(tmp, fail=step, artifact=design())
            got = run(world, inputs(tmp, world, from_artifact=tmp / "d.json", resolved_agy_credential_source="hostuser"))
            check(isinstance(got, SystemExit) and got.code == f"refused at {step}" and world.names() == expected[:expected.index(step) + 1],
                  f"refused at {step}, and nothing after it: {got!r} {world.names()}")


def test_the_answer_cannot_be_changed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        got = run(world, inputs(tmp, world))
        check(isinstance(judged(setattr, got, "worktree_branch", "x"), dataclasses.FrozenInstanceError), "frozen")


# --- the caller ------------------------------------------------------------------------------------------------------


class Fence(Exception):
    """P2 reached: stop there."""


def command_locals(exc: BaseException) -> dict:
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code is t.switchyard_new_command.__code__:
            return dict(tb.tb_frame.f_locals)
        tb = tb.tb_next
    raise AssertionError("the command's frame is not on the way to the fence")


def choices(tmp: Path, log: list) -> object:
    answer = m.NewProjectChoices(**{f.name: object() for f in dataclasses.fields(m.NewProjectChoices)})
    return dataclasses.replace(answer, stages=Stages(log), owner_user=OWNER, project_dir=tmp / "project",
                               resolved_slug="s370", resolved_project_name="N", resolved_agy_credential_source="",
                               selected_implementer_roles=("main",), include_designer=False, include_audit=True,
                               selected_audit_roles=("audit",), selected_role_clis=(("main", "codex"),),
                               runner=lambda *a, **k: None)


def p2_fence(seen: list) -> dict:
    def service_user(user, **kw):
        seen.append(("service-user", user, kw))
        raise Fence()
    return dict(_ensure_board_service_user=service_user, _ensure_board_service_peer_auth=refuse("the peer auth"),
                _ensure_owner_user_and_project_dir=refuse("the owner account"))


def test_the_command_hands_p1_its_values_and_reads_back_each_field() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0 = choices(tmp, log)
        answer = m.NewProjectPreflight(effective_source_repo=object(), precheck_plan=SimpleNamespace(service_user="svc"),
                                       selected_role_clis=object(), worktree_branch=object())
        handed: list = []
        seen: list = []
        given = {name: object() for name in PARAMETERS}
        given.update(home_base=tmp / "homes", from_artifact=None)
        seams = dict(_resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: handed.append(kw) or answer,
                     **p2_fence(seen))
        with patched(t, **seams):
            got = judged(t.switchyard_new_command, **given)
        check(isinstance(got, Fence), f"stopped at P2: {got!r}")
        check(len(handed) == 1 and tuple(handed[0]) == PARAMETERS + FROM_P0, f"P1 called once, with its 27 values: {handed}")
        wrong = [n for n in PARAMETERS if handed[0][n] is not given[n]] + [n for n in FROM_P0 if handed[0][n] is not getattr(p0, n)]
        check(wrong == [], f"each the very value: the command's own, or P0's: {wrong}")
        local = command_locals(got)
        wrong = [f for f in OUTPUTS if local.get(f, Fence) is not getattr(answer, f)]
        check(wrong == [], f"every field is the command's local of that name, the very object: {wrong}")
        check(local["first_run_runner"] is p0.first_run_runner and local["runner"] is p0.runner, "P0's two runners untouched")
        check(log == [("begin", "project accounts and files")] and seen[0][:2] == ("service-user", "svc"),
              f"then P2 begins, with the plan's service user: {log} {seen}")


def test_the_real_phase_between_the_two() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd370.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0 = choices(tmp, log)
        world = World(tmp)
        world.log = log
        seen: list = []
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, **world.seams(), **p2_fence(seen)):
            got = judged(t.switchyard_new_command, source_repo=tmp / "source", home_base=tmp / "homes",
                         config_dir=tmp / "config", registry_dir=tmp / "registry", input_func=refuse("input"),
                         print_func=lambda line: None)
        check(isinstance(got, Fence), f"stopped at P2: {got!r}")
        precheck = next(e[2] for e in log if e[0] == "precheck")
        check(precheck["runner"] is p0.runner, "the precheck got P0's plain runner")
        local = command_locals(got)
        check(local["selected_role_clis"] is world.gated and local["worktree_branch"] == "main"
              and local["effective_source_repo"] == (tmp / "source").resolve(strict=False),
              "the gate's roles, the branch and the source are the command's from here on")
        check([e[0] for e in log] == ["begin", "path", "plan", "precheck", "sources", "gate", "begin"] and log[-1] == ("begin", "project accounts and files"),
              f"P1 then P2's stage, and nothing of P2 past its fence: {[e[0] for e in log]}")
        check(seen[0][1] == local["precheck_plan"].service_user and seen[0][2]["runner"] is p0.runner,
              "P2's first step gets P1's plan and P0's runner")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_and_nothing_bound_read_through_the_launcher")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"new_project_preflight_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
