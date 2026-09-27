#!/usr/bin/env python3
"""SYRD-369: `switchyard new`'s choice phase, against the command it came out of.

The first nineteen statements of `switchyard_new_command` -- the runner
normalization through the progress stages -- moved into
`_resolve_new_project_choices` in `scripts/new_project_phases.py`, which returns
a frozen `NewProjectChoices`; the command calls it by the launcher's name and
assigns each of the twenty-one fields to the local it always had. This pins
what makes that safe:

- **No cycle, one set of objects.** The module imports only the standard
  library at its top; the launcher re-exports the phase and its result
  whichever module is imported first.
- **Seams (rule 24).** Every launcher facility the phase uses is read from the
  launcher when it runs, as often as the command read it; nothing the phase
  binds is read through it, and the desktop-access validation is still imported
  inside the phase, where the command imported it.
- **The runner.** The one statement that tells "nobody injected a runner" from
  "somebody injected `subprocess.run`" is still one statement: the first-run
  phase gets exactly what the caller passed, everything else the plain runner.
- **The behaviour is unchanged:** the design-artifact and the asked-for paths,
  the supplied and the guided role plans, every refusal in its order with
  nothing after it, and every value the rest of the command reads.
- **The caller.** `switchyard_new_command` hands the phase its own values and
  reads back every field as the very object the phase returned.

Every facility is this test's own fake: each launcher seam, the desktop-policy
validation, the prompts, the euid, the role plan and the owner's home. Nothing
is provisioned, looked up or run; the only paths are inside temporary
directories this test owns, and the command is stopped by a fake at its second
phase before anything there could act.
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
from scripts import desktop_access  # noqa: E402

CHECKS = 0
#: Measured on the baseline command's first nineteen statements: each launcher
#: name the phase reads, and how many times.
SEAMS = {
    "NEW_PROJECT_RESERVED_ROLE_NAMES": 1, "NEW_PROJECT_STAGES": 1, "PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS": 2,
    "ProvisioningStages": 1, "SWITCHYARD_DESIGN_FILE_NAME": 1, "SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME": 1,
    "_NoRunnerInjected": 1, "_agent_owner_user": 1, "_check_switchyard_registration_available": 1,
    "_confirm_existing_owner_user": 1, "_confirm_switchyard_new": 1, "_dedupe_role_cli_pairs": 1,
    "_owner_home_for_auth": 1, "_owner_user_verbatim": 1, "_project_dir": 1, "_prompt_switchyard_role_plan": 1,
    "_prompt_text": 4, "_require_new_project_roles": 1, "_resolve_agy_credential_source": 1,
    "_resolve_desktop_policy": 1, "_resolve_project_path": 2, "_slug_from_project_name": 1, "_switchyard_dir": 2,
    "_validate_project_slug": 1, "load_project_design_artifact": 1, "print_role_plan_review": 1,
}
INPUTS = ("slug", "agent_name", "project_name", "project_path", "from_artifact", "role_clis", "yes", "desktop_policy",
          "headless", "desktop_gui_user", "desktop_approval_settings_path", "allow_existing_owner_user",
          "agy_credential_source", "no_agy_credential", "agy_credential_settings_path", "home_base", "euid_getter",
          "runner", "config_dir", "registry_dir", "input_func", "print_func")
OUTPUTS = ("agy_source_origin", "artifact_path", "design_document", "director_onboarding", "first_run_runner",
           "include_audit", "include_designer", "owner_shell", "owner_user", "project_dir",
           "resolved_agy_credential_source", "resolved_project_name", "resolved_slug", "runner",
           "selected_audit_roles", "selected_desktop_policy", "selected_implementer_roles", "selected_role_clis",
           "selected_role_efforts", "selected_role_models", "stages")
OWN = ("subprocess", "dataclass", "Path", "Any", "Callable", "Sequence")
OWNER = "syrd369-agent"


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
    """The progress stages, recorded rather than printed."""

    def __init__(self, log: list, names, *, print_func) -> None:
        self.names, self.print_func = names, print_func
        log.append(("stages", names, print_func))
        self.log = log

    def begin(self, name: str, **kwargs: object) -> None:
        self.log.append(("begin", name))


class World:
    """One run's fakes: every launcher seam the phase reads, recording in order."""

    def __init__(self, tmp: Path, **answers: object) -> None:
        self.tmp, self.log = tmp, []
        self.answers = {"Project name": "  Syrd Three  ", "Slug": "syrd369", "Agent user": f" {OWNER} ",
                        "Project path": str(tmp / "chosen")}
        self.answers.update(answers.pop("prompts", {}))
        self.fail = answers.pop("fail", None)
        self.plan = answers.pop("plan", [SimpleNamespace(role="director", cli="claude", model="opus", effort="high"),
                                          SimpleNamespace(role="main", cli="codex", model="", effort="low"),
                                          SimpleNamespace(role="audit", cli="claude", model="sonnet", effort="")])
        self.artifact = answers.pop("artifact", None)
        self.validated = {"validated": True}
        self.desktop = {"raw": True}
        self.auth_home = tmp / "owner-home"
        assert not answers, answers

    def step(self, name: str, *detail: object) -> None:
        self.log.append((name, *detail))
        if self.fail == name:
            raise SystemExit(f"refused at {name}")

    def seams(self) -> dict:
        L = self

        def prompt(label, default=None, *, input_func):
            L.step("prompt", label, default, input_func)
            return L.answers[label]

        def plan(**kw):
            L.step("plan", kw)
            return L.plan

        return dict(
            load_project_design_artifact=lambda path: L.step("artifact", path) or L.artifact,
            _resolve_project_path=lambda raw: L.step("path", raw) or Path(str(raw)),
            _prompt_text=prompt,
            _slug_from_project_name=lambda name: L.step("slugify", name) or "slug-default",
            _validate_project_slug=lambda value: L.step("slug", value) or value,
            _agent_owner_user=lambda slug: L.step("agent-default", slug) or "agent-default",
            _owner_user_verbatim=lambda raw: L.step("verbatim", raw) or raw.strip(),
            _project_dir=lambda base, owner, name: L.step("project-dir", base, owner, name) or base / owner / "p",
            _resolve_agy_credential_source=lambda **kw: L.step("agy", kw) or ("agy-src", "agy-origin"),
            _resolve_desktop_policy=lambda **kw: L.step("desktop", kw) or (L.desktop, "desktop-origin"),
            _check_switchyard_registration_available=lambda **kw: L.step("registration", kw),
            _confirm_existing_owner_user=lambda owner, **kw: L.step("existing-owner", owner, kw),
            _confirm_switchyard_new=lambda **kw: L.step("confirm", kw),
            _prompt_switchyard_role_plan=plan,
            _owner_home_for_auth=lambda owner, *, fallback: L.step("auth-home", owner, fallback) or L.auth_home,
            print_role_plan_review=lambda role_plan, *, print_func: L.step("review", role_plan, print_func),
            _dedupe_role_cli_pairs=lambda pairs: L.step("dedupe", pairs) or tuple(dict.fromkeys(tuple(p) for p in pairs)),
            _require_new_project_roles=lambda clis: L.step("require", clis),
            _switchyard_dir=lambda project_dir: project_dir / ".switchyard",
            ProvisioningStages=lambda names, *, print_func: Stages(L.log, names, print_func=print_func),
        )

    def validate(self, raw, *, project, tenant):
        self.step("validate", raw, project, tenant)
        return self.validated

    def names(self) -> list:
        return [entry[0] for entry in self.log]


def inputs(tmp: Path, **overrides: object) -> dict:
    said: list = []
    values = dict(slug=None, agent_name=None, project_name=None, project_path=None, from_artifact=None, role_clis=None,
                  yes=False, desktop_policy=None, headless=False, desktop_gui_user=None,
                  desktop_approval_settings_path=None, allow_existing_owner_user=False, agy_credential_source=None,
                  no_agy_credential=False, agy_credential_settings_path=None, home_base=tmp / "homes",
                  euid_getter=lambda: 0, runner=t.NO_RUNNER_INJECTED, config_dir=tmp / "config",
                  registry_dir=tmp / "registry", input_func=refuse("the terminal"), print_func=said.append)
    values.update(overrides)
    return values


def run(world: World, values: dict) -> object:
    with patched(t, **world.seams()), patched(desktop_access, validate_policy=world.validate):
        return judged(m._resolve_new_project_choices, **values)


def artifact(tmp: Path, **fields: object) -> SimpleNamespace:
    base = dict(project="art369", project_name="Art Three", owner_user="art-agent", repository=tmp / "repo",
                capability_grants={"shell": "/bin/zsh", "agy_credential_source": "  hostuser  "},
                role_clis=(("director", "claude"), ("main", "codex")), implementer_roles=("main",),
                include_designer=False, include_audit=False, audit_roles=())
    base.update(fields)
    return SimpleNamespace(**base)


def phase_def() -> ast.FunctionDef:
    tree = ast.parse((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_resolve_new_project_choices")


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
                        "print(t._resolve_new_project_choices is m._resolve_new_project_choices, t.NewProjectChoices is m.NewProjectChoices, "
                        f"all(getattr(t, n) is getattr(m, n) for n in {OWN!r} if hasattr(t, n)))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_and_nothing_bound_read_through_the_launcher() -> None:
    fn = phase_def()
    check(isinstance(fn.body[0], ast.ImportFrom) and fn.body[0].module == "scripts"
          and [(a.name, a.asname) for a in fn.body[0].names] == [("team_launcher", "launcher")],
          "the launcher is imported first, when the phase runs")
    module = ast.parse((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "and never at the module's top at run time")
    through: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "launcher":
            through[node.attr] = through.get(node.attr, 0) + 1
    check(through == SEAMS, f"each launcher name read through it exactly as often as the command read it: {through}")
    # Annotations are never evaluated here (`from __future__ import annotations`).
    written = [a.annotation for a in ast.walk(fn) if isinstance(a, (ast.AnnAssign, ast.arg)) and a.annotation is not None]
    annotations = {id(n) for a in [*written, fn.returns] for n in ast.walk(a)}
    bare = sorted({n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in SEAMS and id(n) not in annotations})
    check(bare == [], f"and none of them read past it: {bare}")
    bound = {a.arg for a in fn.args.kwonlyargs} | {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    check(not bound & set(through), f"nothing the phase binds is read through the launcher: {bound & set(through)}")
    local = [(i, ast.unparse(s)) for i, s in enumerate(fn.body) if isinstance(s, (ast.Import, ast.ImportFrom))]
    decided = [i for i, s in enumerate(fn.body) if "launcher._resolve_desktop_policy(" in ast.unparse(s)]
    check(local == [(0, "from scripts import team_launcher as launcher"), (decided[0] + 1, "from scripts.desktop_access import validate_policy")]
          and "validate_policy(" in ast.unparse(fn.body[decided[0] + 2]),
          f"the desktop validation is imported inside the phase, right after the policy is decided, then used: {local}")


def test_the_one_statement_the_runner_depends_on() -> None:
    fn = phase_def()
    first = fn.body[1]
    check(ast.unparse(first) == "first_run_runner, runner = (runner, subprocess.run if isinstance(runner, launcher._NoRunnerInjected) else runner)",
          f"one simultaneous assignment, first: {ast.unparse(first)}")
    stores = [i for i, s in enumerate(fn.body) for n in ast.walk(s)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in ("runner", "first_run_runner")]
    check(stores == [1, 1], f"and nothing else in the phase rebinds either: {stores}")
    check(m.subprocess is t.subprocess, "the phase's subprocess is the launcher's module")


def test_the_interface_and_the_result() -> None:
    fn = phase_def()
    args = fn.args
    check(not args.args and not args.posonlyargs and not args.vararg and not args.kwarg
          and tuple(a.arg for a in args.kwonlyargs) == INPUTS and all(d is None for d in args.kw_defaults),
          f"keyword-only, the command's order, no defaults: {[a.arg for a in args.kwonlyargs]}")
    command = next(n for n in ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")).body
                   if isinstance(n, ast.FunctionDef) and n.name == "switchyard_new_command")
    theirs = {a.arg: ast.unparse(a.annotation) for a in command.args.kwonlyargs}
    check(all(ast.unparse(a.annotation) == theirs[a.arg] for a in args.kwonlyargs), "each with the command's own annotation")
    fields = dataclasses.fields(m.NewProjectChoices)
    check(tuple(f.name for f in fields) == OUTPUTS, f"the twenty-one values, in order: {[f.name for f in fields]}")
    check(all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields),
          "no field has a default, so none can be manufactured")
    check(m.NewProjectChoices.__dataclass_params__.frozen, "frozen")
    returned = fn.body[-1]
    check(isinstance(returned, ast.Return) and [k.arg for k in returned.value.keywords] == list(OUTPUTS)
          and all(isinstance(k.value, ast.Name) and k.value.id == k.arg for k in returned.value.keywords),
          "and the phase returns each from the local of the same name")
    check(sum(isinstance(n, ast.Return) for n in ast.walk(fn)) == 1, "with no other way out but a refusal")


def test_the_command_hands_its_own_values_and_reads_every_field_back() -> None:
    command = next(n for n in ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")).body
                   if isinstance(n, ast.FunctionDef) and n.name == "switchyard_new_command")
    call = command.body[0]
    check(ast.unparse(call.value.func) == "_resolve_new_project_choices" and not call.value.args
          and [(k.arg, ast.unparse(k.value)) for k in call.value.keywords] == [(n, n) for n in INPUTS],
          "the phase first, called by the launcher's name, with the command's own values")
    back = [ast.unparse(s) for s in command.body[1:1 + len(OUTPUTS)]]
    check(back == [f"{f} = new_project_choices.{f}" for f in OUTPUTS], f"then every field, to its old name: {back[:3]}")
    check(ast.unparse(command.body[1 + len(OUTPUTS)]) == "stages.begin('host and agent CLI checks')",
          "then the host checks begin, where they always did")


# --- behaviour -------------------------------------------------------------------------------------------------------


def test_the_asked_for_path() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        asked: list = []
        values = inputs(tmp, input_func=asked.append, role_clis=(("director", "claude"), ("main", "codex")))
        got = run(world, values)
        check(isinstance(got, m.NewProjectChoices), f"it goes on: {got!r}")
        prompts = [(e[1], e[2], e[3]) for e in world.log if e[0] == "prompt"]
        check(prompts == [("Project name", None, asked.append), ("Slug", "slug-default", asked.append),
                          ("Agent user", "agent-default", asked.append),
                          ("Project path", str(tmp / "homes" / OWNER / "p"), asked.append)],
              f"name, slug, agent and path asked for, each with its default and the caller's input: {prompts}")
        check(got.resolved_project_name == "Syrd Three", "the name stripped")
        check(("slugify", "Syrd Three") in world.log and ("agent-default", "syrd369") in world.log,
              "the slug default from the name, the agent default from the slug")
        check(("verbatim", f" {OWNER} ") in world.log and got.owner_user == OWNER, "the owner named verbatim")
        check(("project-dir", tmp / "homes", OWNER, "Syrd Three") in world.log, "the default path under the home base")
        check(got.project_dir == tmp / "chosen" and got.resolved_slug == "syrd369", "and the answers kept")
        check(got.owner_shell == str(t.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"]), "the default shell")


def test_what_the_caller_supplied_is_not_asked_for() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        got = run(world, inputs(tmp, project_name="Given", slug="given-slug", agent_name="", project_path=tmp / "given",
                                role_clis=(("main", "codex"),)))
        check(isinstance(got, m.NewProjectChoices) and "prompt" not in world.names(),
              f"nothing asked: {[e for e in world.log if e[0] == 'prompt']}")
        check(("slug", "given-slug") in world.log and ("verbatim", "") in world.log and got.project_dir == tmp / "given",
              "an empty agent name is still the caller's, not a question")


def test_an_empty_name_is_refused_before_anything_else() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp, prompts={"Project name": "   "})
        got = run(world, inputs(tmp, input_func=lambda _: ""))
        check(isinstance(got, SystemExit) and got.code == "switchyard: project name cannot be empty", f"refused: {got!r}")
        check(world.names() == ["prompt"], f"and nothing after it: {world.names()}")


def test_the_design_artifact_path() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        design = artifact(tmp)
        world = World(tmp, artifact=design)
        got = run(world, inputs(tmp, from_artifact=tmp / "art.project.json", role_clis=(("x", "y"),)))
        check(isinstance(got, m.NewProjectChoices), f"it goes on: {got!r}")
        check(world.log[0] == ("artifact", tmp / "art.project.json") and "prompt" not in world.names(),
              "the artifact loaded first and nothing asked")
        check((got.resolved_slug, got.resolved_project_name, got.owner_user, got.owner_shell)
              == ("art369", "Art Three", "art-agent", "/bin/zsh"), "slug, name, owner and shell from the artifact")
        check(("path", tmp / "repo") in world.log and got.project_dir == tmp / "repo", "the artifact's repository")
        check(got.selected_role_clis is design.role_clis and got.selected_implementer_roles is design.implementer_roles
              and got.selected_audit_roles is design.audit_roles and got.include_designer is False and got.include_audit is False,
              "and its roles, not the caller's and not a plan")
        check(not {"plan", "dedupe", "require"} & set(world.names()), "no role plan at all")
        check(got.selected_role_models == {} and got.selected_role_efforts == {}, "no models or efforts")
        agy = next(e[1] for e in world.log if e[0] == "agy")
        check(agy["artifact_value"] == "hostuser", f"the artifact's agy source, stripped: {agy['artifact_value']!r}")
        check(got.artifact_path == (tmp / "art.project.json").resolve(strict=False), "the artifact is the artifact path")
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp, artifact=artifact(tmp, capability_grants={}))
        got = run(world, inputs(tmp, from_artifact=tmp / "a.json", project_path=tmp / "over"))
        check(got.project_dir == tmp / "over" and ("path", tmp / "over") in world.log, "the caller's path over the artifact's")
        check(got.owner_shell == str(t.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"]), "the default shell when none granted")
        check(next(e[1] for e in world.log if e[0] == "agy")["artifact_value"] == "", "and no agy source")


def test_the_credential_source_and_the_desktop_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        said: list = []
        values = inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p", yes=True,
                        agy_credential_source="over", no_agy_credential=True, agy_credential_settings_path=tmp / "agy",
                        desktop_policy=tmp / "policy", headless=True, desktop_gui_user=None,
                        desktop_approval_settings_path=tmp / "approval", input_func=said.append, print_func=said.append,
                        role_clis=(("main", "codex"),))
        got = run(world, values)
        agy = next(e[1] for e in world.log if e[0] == "agy")
        check(agy == dict(override="over", opt_out=True, artifact_value="", home_base=tmp / "homes", settings_path=tmp / "agy",
                          yes=True, input_func=said.append, print_func=said.append), f"the agy source asked as before: {agy}")
        check((got.resolved_agy_credential_source, got.agy_source_origin) == ("agy-src", "agy-origin"), "and kept")
        desk = next(e[1] for e in world.log if e[0] == "desktop")
        check(desk == dict(desktop_policy=tmp / "policy", headless=True, gui_user="", project="s369", tenant=OWNER, yes=True,
                           input_func=said.append, print_func=said.append, settings_path=tmp / "approval"),
              f"the desktop decided as before, no GUI user read as empty: {desk}")
        check(("validate", world.desktop, "s369", OWNER) in world.log and got.selected_desktop_policy is world.validated,
              "then validated, and the validated policy is the one kept")


def test_the_order_and_every_refusal_stops_what_follows() -> None:
    expected = ["agy", "desktop", "validate", "registration", "existing-owner", "confirm", "auth-home", "plan", "review",
                "dedupe", "require"]
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p"))
        order = [n for n in world.names() if n in expected]
        check(isinstance(got, m.NewProjectChoices) and order == expected, f"the order: {order}")
        registration = next(e[1] for e in world.log if e[0] == "registration")
        check(registration == dict(slug="s369", name="N", config_dir=tmp / "config", registry_dir=tmp / "registry"),
              f"registration asked of the caller's directories: {registration}")
        owner = next(e for e in world.log if e[0] == "existing-owner")
        check(owner[1] == OWNER and owner[2]["agy_credential_source"] == "agy-src" and owner[2]["allow_existing_owner_user"] is False,
              f"an existing owner confirmed with the resolved agy source: {owner}")
        confirm = next(e[1] for e in world.log if e[0] == "confirm")
        check(confirm["project_dir"] == tmp / "p" and confirm["owner_user"] == OWNER and confirm["yes"] is False,
              f"then the new project confirmed: {confirm}")
    for step in ("agy", "desktop", "validate", "registration", "existing-owner", "confirm", "plan", "require"):
        with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
            tmp = Path(tmp)
            world = World(tmp, fail=step)
            got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p"))
            names = [n for n in world.names() if n in expected]
            check(isinstance(got, SystemExit) and got.code == f"refused at {step}" and names == expected[:expected.index(step) + 1],
                  f"refused at {step}, and nothing after it: {got!r} {names}")


def test_only_root_goes_on_to_the_roles() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p", euid_getter=lambda: 1000))
        check(isinstance(got, SystemExit) and got.code == "switchyard: new requires sudo; re-run as `sudo ./switchyard new`",
              f"refused as not root: {got!r}")
        check(world.names()[-1] == "confirm" and "plan" not in world.names(), f"after the confirmation, before any role: {world.names()}")


def test_supplied_roles() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        pairs = [("director", "claude"), ("designer", "codex"), ("main", "codex"), ("main", "codex"), ("audit", "claude")]
        got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p", role_clis=pairs))
        check(("dedupe", pairs) in world.log and "plan" not in world.names() and "review" not in world.names(),
              "the caller's pairs, deduplicated, with no plan asked for")
        check(got.selected_role_clis == (("director", "claude"), ("designer", "codex"), ("main", "codex"), ("audit", "claude")),
              f"deduplicated: {got.selected_role_clis}")
        check(("require", got.selected_role_clis) in world.log, "the required roles checked")
        check(got.selected_implementer_roles == ("main",), f"reserved roles are not implementers: {got.selected_implementer_roles}")
        check((got.include_designer, got.include_audit, got.selected_audit_roles) == (True, True, ("audit",)), "designer and audit")
        check(got.selected_role_models == {} and got.selected_role_efforts == {}, "no models or efforts")
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p",
                                role_clis=[("director", "claude")]))
        check(isinstance(got, SystemExit) and got.code == "switchyard: at least one implementer role is required",
              f"no implementer is refused: {got!r}")
        check(world.names()[-1] == "require", "after the required roles")
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p",
                                role_clis=[("director", "claude"), ("main", "codex")]))
        check((got.include_designer, got.include_audit, got.selected_audit_roles) == (False, False, ()), "no designer, no audit")


def test_the_guided_role_plan() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        said: list = []
        custom = lambda *a, **k: None  # noqa: E731
        got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p",
                                runner=custom, input_func=said.append, print_func=said.append))
        plan = next(e[1] for e in world.log if e[0] == "plan")
        check(plan == dict(runner=custom, owner_user=OWNER, owner_home=world.auth_home, input_func=said.append, print_func=said.append),
              f"the plan asked with the runner, the owner's home and the caller's input and output: {plan}")
        check(("auth-home", OWNER, tmp / "homes" / OWNER) in world.log, "the owner's home, falling back under the home base")
        check(("review", world.plan, said.append) in world.log and world.names().index("review") < world.names().index("dedupe"),
              "the plan reviewed before the roles are settled")
        check(("dedupe", [("director", "claude"), ("main", "codex"), ("audit", "claude")]) in world.log, "the plan's pairs")
        check(got.selected_role_models == {"director": "opus", "audit": "sonnet"} and got.selected_role_efforts == {"director": "high", "main": "low"},
              f"only the models and efforts the plan named: {got.selected_role_models} {got.selected_role_efforts}")


def test_the_runner_the_rest_of_the_command_gets() -> None:
    custom = lambda *a, **k: None  # noqa: E731
    for given, first, plain in ((t.NO_RUNNER_INJECTED, t.NO_RUNNER_INJECTED, subprocess.run),
                                (subprocess.run, subprocess.run, subprocess.run), (custom, custom, custom)):
        with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
            tmp = Path(tmp)
            world = World(tmp)
            got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p", runner=given))
            check(got.first_run_runner is first and got.runner is plain,
                  f"given {given!r}: the first-run phase gets {got.first_run_runner!r}, the rest {got.runner!r}")
            check(next(e[1] for e in world.log if e[0] == "plan")["runner"] is plain, "and the role plan the plain one")


def test_the_paths_and_the_stages() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        world = World(tmp)
        said: list = []
        got = run(world, inputs(tmp, project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p", print_func=said.append,
                                role_clis=(("main", "codex"),)))
        check(got.artifact_path == (tmp / "p" / ".switchyard" / "s369.project.json").resolve(strict=False),
              f"the artifact path in the project's switchyard directory: {got.artifact_path}")
        check(got.design_document == tmp / "p" / t.SWITCHYARD_DESIGN_FILE_NAME, "the design document")
        check(got.director_onboarding == tmp / "p" / ".switchyard" / t.SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME, "the onboarding")
        check(isinstance(got.stages, Stages) and got.stages.names is t.NEW_PROJECT_STAGES and got.stages.print_func == said.append,
              "the progress stages, printing where the caller prints")
        check(world.log[-1][0] == "stages" and not [e for e in world.log if e[0] == "begin"], "built last, and not begun here")
        attempt = judged(setattr, got, "runner", None)
        check(isinstance(attempt, dataclasses.FrozenInstanceError), f"and the answer cannot be changed: {attempt!r}")


# --- the caller ------------------------------------------------------------------------------------------------------


class Fence(Exception):
    """The command's second phase reached: stop there."""


def fenced(extra: dict | None = None) -> tuple[dict, list]:
    seen: list = []

    def precheck(plan, **kwargs):
        seen.append(("precheck", kwargs["runner"]))
        raise Fence()

    seams = dict(_precheck_project_path_before_mutating=lambda owner, project_dir: seen.append(("path-precheck", owner, project_dir)),
                 _repo_root=refuse("the source checkout"), build_plan=lambda **kw: SimpleNamespace(**kw),
                 precheck_new_project=precheck)
    seams.update(extra or {})
    return seams, seen


def command_locals(exc: BaseException) -> dict:
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code is t.switchyard_new_command.__code__:
            return dict(tb.tb_frame.f_locals)
        tb = tb.tb_next
    raise AssertionError("the command's frame is not on the way to the fence")


def test_the_command_reads_back_every_field_as_the_phase_returned_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
        tmp = Path(tmp)
        stages_log: list = []
        answer = m.NewProjectChoices(**{f: object() for f in OUTPUTS})
        answer = dataclasses.replace(answer, stages=Stages(stages_log, (), print_func=None), owner_user=OWNER,
                                     project_dir=tmp / "p", resolved_slug="s369", resolved_project_name="N",
                                     selected_implementer_roles=("main",), include_designer=True, include_audit=True,
                                     selected_audit_roles=("audit",))
        handed: list = []
        seams, seen = fenced({"_resolve_new_project_choices": lambda **kw: handed.append(kw) or answer})
        # Each of the phase's inputs its own object, so a value handed to the wrong parameter shows.
        given = {name: object() for name in INPUTS if name != "runner"}
        given.update(home_base=tmp / "homes", from_artifact=None)
        with patched(t, **seams):
            got = judged(t.switchyard_new_command, source_repo=tmp / "source", **given)
        check(isinstance(got, Fence), f"stopped at the second phase: {got!r}")
        check(len(handed) == 1 and tuple(handed[0]) == INPUTS, f"the phase called once, with its twenty-two values: {handed}")
        wrong = [name for name in given if handed[0][name] is not given[name]]
        check(wrong == [], f"each the very value the command was given: {wrong}")
        check(handed[0]["runner"] is t.NO_RUNNER_INJECTED, "nobody injected a runner, and the phase is told so")
        local = command_locals(got)
        wrong = [f for f in OUTPUTS if local.get(f, Fence) is not getattr(answer, f)]
        check(wrong == [], f"every field is the command's local of that name, the very object: {wrong}")
        check(seen == [("path-precheck", OWNER, tmp / "p"), ("precheck", answer.runner)] and stages_log[-1] == ("begin", "host and agent CLI checks"),
              f"and the second phase starts with them: {seen} {stages_log[-1:]}")


def test_the_runner_across_the_boundary_for_real() -> None:
    custom = lambda *a, **k: None  # noqa: E731
    for given, first, plain in ((None, t.NO_RUNNER_INJECTED, subprocess.run), (subprocess.run, subprocess.run, subprocess.run),
                                (custom, custom, custom)):
        with tempfile.TemporaryDirectory(prefix="syrd369.") as tmp:
            tmp = Path(tmp)
            world = World(tmp)
            seams, seen = fenced(world.seams())
            options = dict(project_name="N", slug="s369", agent_name=OWNER, project_path=tmp / "p", source_repo=tmp / "source",
                           home_base=tmp / "homes", config_dir=tmp / "config", registry_dir=tmp / "registry",
                           euid_getter=lambda: 0, input_func=refuse("input"), print_func=lambda _: None,
                           role_clis=(("main", "codex"),))
            if given is not None:
                options["runner"] = given
            with patched(t, **seams), patched(desktop_access, validate_policy=world.validate):
                got = judged(t.switchyard_new_command, **options)
            check(isinstance(got, Fence), f"stopped at the second phase: {got!r}")
            local = command_locals(got)
            check(local["first_run_runner"] is first and local["runner"] is plain and seen[-1] == ("precheck", plain),
                  f"given {given!r}: first-run {local['first_run_runner']!r}, the rest {local['runner']!r}, the host checks {seen[-1]!r}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_and_nothing_bound_read_through_the_launcher")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"new_project_phases_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
