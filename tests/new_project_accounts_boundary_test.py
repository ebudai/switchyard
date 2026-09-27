#!/usr/bin/env python3
"""SYRD-371: `switchyard new`'s accounts-and-files phase, against the command it came out of.

The eleven statements from `stages.begin("project accounts and files")`
through the desktop policy's write moved into `_prepare_new_project_accounts`
in `scripts/new_project_phases.py`, which returns a frozen `NewProjectAccounts`;
the command calls it where they were and reads back `provision_dir`. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first.
- **Seams (rule 24).** Every launcher facility the phase uses is read from the
  launcher when it runs, as often as the command read it; nothing it binds is,
  and `Path` is the module's own.
- **The behaviour is unchanged:** the board service user, its peer
  authentication and the owner account, in that order; the owner's CLIs
  verified only once the account exists; what is said about the account; the
  agy credential left, refused or seeded; the fresh and the design-artifact
  branches with every field they write, ownership, the documents and every git
  variant; and last the provisioning directory with the desktop policy.
- **The caller.** The command hands the phase its own values and the earlier
  phases' objects, reads back `provision_dir` as returned, and goes on to P3.

Every facility is this test's own fake: each launcher seam and the stages. No
account, credential, ownership, repository or document is touched; the only
filesystem effects are the provisioning directory created inside a temporary
directory this test owns, and the command is stopped by a fake at P3's first
step, before anything there could act.
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
#: Measured on the baseline command's eleven P2 statements.
SEAMS = {"AGY_CREDENTIAL_DIR_NAME": 1, "AGY_CREDENTIAL_INSTALLED": 1, "AGY_CREDENTIAL_TOKEN_NAME": 1,
         "AGY_CREDENTIAL_UNUSABLE": 1, "PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS": 1, "_agy_credential_state": 1,
         "_chown_project_file": 1, "_chown_switchyard_project_files": 2, "_ensure_board_service_peer_auth": 1,
         "_ensure_board_service_user": 1, "_ensure_owner_user_and_project_dir": 1, "_ensure_project_git_repository": 2,
         "_install_switchyard_onboarding_docs": 2, "_require_existing_project_git_repository": 2,
         "_seed_agy_credential_for_owner": 1, "_switchyard_dir": 1, "_write_initial_switchyard_project_artifact": 1,
         "_write_json_atomic": 1, "_write_switchyard_onboarding_files": 2, "load_project_design_artifact": 1,
         "verify_agent_clis_for_owner": 1}
PARAMETERS = ("from_artifact", "output_dir", "home_base", "git_init", "print_func")
FROM_P0 = ("agy_source_origin", "artifact_path", "design_document", "director_onboarding", "include_audit",
           "include_designer", "owner_shell", "owner_user", "project_dir", "resolved_agy_credential_source",
           "resolved_project_name", "resolved_slug", "runner", "selected_audit_roles", "selected_desktop_policy",
           "selected_implementer_roles", "selected_role_efforts", "selected_role_models", "stages")
FROM_P1 = ("effective_source_repo", "precheck_plan", "selected_role_clis", "worktree_branch")
OWNER = "syrd371-agent"


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

    def __init__(self, tmp: Path, *, fail: str | None = None, created: bool = True, shell_path: str | None = "/usr/bin/fish",
                 agy_state: str = "absent", git_created: bool = True, artifact: object = None) -> None:
        self.tmp, self.fail, self.log = tmp, fail, []
        self.owner = SimpleNamespace(created=created, shell_path=shell_path)
        self.agy_state, self.git_created = agy_state, git_created
        self.artifact = artifact or SimpleNamespace(design_document=tmp / "artifact-design.md", include_designer=True)

    def step(self, name: str, *detail: object) -> None:
        self.log.append((name, *detail))
        if self.fail == name:
            raise SystemExit(f"refused at {name}")

    def seams(self) -> dict:
        L = self
        return dict(
            _ensure_board_service_user=lambda user, *, runner: L.step("service-user", user, runner),
            _ensure_board_service_peer_auth=lambda plan, *, source_repo, runner: L.step("peer-auth", plan, source_repo, runner),
            _ensure_owner_user_and_project_dir=lambda owner, project_dir, **kw: L.step("owner", owner, project_dir, kw) or L.owner,
            verify_agent_clis_for_owner=lambda clis, **kw: L.step("verify", clis, kw),
            _agy_credential_state=lambda owner, home: L.step("agy-state", owner, home) or L.agy_state,
            _seed_agy_credential_for_owner=lambda **kw: L.step("agy-seed", kw),
            _write_switchyard_onboarding_files=lambda **kw: L.step("onboarding", kw),
            _write_initial_switchyard_project_artifact=lambda **kw: L.step("artifact-write", kw),
            _chown_switchyard_project_files=lambda **kw: L.step("chown-files", kw),
            _chown_project_file=lambda **kw: L.step("chown-design", kw),
            _install_switchyard_onboarding_docs=lambda **kw: L.step("docs", kw),
            _ensure_project_git_repository=lambda **kw: L.step("git-init", kw) or L.git_created,
            _require_existing_project_git_repository=lambda **kw: L.step("git-require", kw),
            load_project_design_artifact=lambda path: L.step("artifact-load", path) or L.artifact,
            _switchyard_dir=lambda project_dir: project_dir / ".switchyard",
            _write_json_atomic=lambda path, payload: L.step("policy", path, payload),
        )

    def names(self) -> list:
        return [entry[0] for entry in self.log]


def inputs(tmp: Path, world: World, said: list, **overrides: object) -> dict:
    values = dict(from_artifact=None, output_dir=None, home_base=tmp / "homes", git_init=True, print_func=said.append,
                  agy_source_origin="host", artifact_path=tmp / "project" / ".switchyard" / "s371.project.json",
                  design_document=tmp / "project" / "DESIGN.md", director_onboarding=tmp / "project" / ".switchyard" / "director.md",
                  include_audit=True, include_designer=False, owner_shell="fish", owner_user=OWNER, project_dir=tmp / "project",
                  resolved_agy_credential_source="", resolved_project_name="Syrd Three Seventy-One", resolved_slug="s371",
                  runner=lambda *a, **k: None, selected_audit_roles=("audit",), selected_desktop_policy={"mode": "headless"},
                  selected_implementer_roles=("main",), selected_role_efforts={"main": "high"},
                  selected_role_models={"main": "opus"}, stages=Stages(world.log),
                  effective_source_repo=tmp / "source", precheck_plan=SimpleNamespace(service_user="s371-board"),
                  selected_role_clis=(("director", "claude"), ("main", "codex")), worktree_branch="trunk")
    values.update(overrides)
    return values


def run(world: World, values: dict) -> object:
    with patched(t, **world.seams()):
        return judged(m._prepare_new_project_accounts, **values)


def module_tree() -> ast.Module:
    return ast.parse((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"))


def phase_def() -> ast.FunctionDef:
    return next(n for n in module_tree().body if isinstance(n, ast.FunctionDef) and n.name == "_prepare_new_project_accounts")


def command_def() -> ast.FunctionDef:
    return next(n for n in ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")).body
                if isinstance(n, ast.FunctionDef) and n.name == "switchyard_new_command")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.new_project_phases as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    names = ("_prepare_new_project_accounts", "NewProjectAccounts", "_check_new_project_preflight", "_resolve_new_project_choices")
    for order in (("scripts.new_project_phases", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.new_project_phases")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.new_project_phases as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {names!r}), t.Path is m.Path)")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


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
    check("Path" not in through and m.Path is Path, "`Path` is the module's own, the standard library's")
    check(ast.unparse(fn.body[1]) == "stages.begin('project accounts and files')", "the stage begun first")
    check(ast.unparse(fn.body[-2]).startswith("launcher._write_json_atomic(provision_dir / 'desktop-policy.json'"),
          "and the desktop policy written last")


def test_the_interface_and_the_result() -> None:
    fn = phase_def()
    args = fn.args
    check(not args.args and not args.posonlyargs and not args.vararg and not args.kwarg
          and tuple(a.arg for a in args.kwonlyargs) == PARAMETERS + FROM_P0 + FROM_P1 and all(d is None for d in args.kw_defaults),
          f"keyword-only: the command's parameters, P0's then P1's values; no defaults: {[a.arg for a in args.kwonlyargs]}")
    theirs = {a.arg: ast.unparse(a.annotation) for a in command_def().args.kwonlyargs}
    for cls in ("NewProjectChoices", "NewProjectPreflight"):
        node = next(n for n in module_tree().body if isinstance(n, ast.ClassDef) and n.name == cls)
        theirs.update({s.target.id: ast.unparse(s.annotation) for s in node.body if isinstance(s, ast.AnnAssign)})
    check(all(ast.unparse(a.annotation) == theirs[a.arg] for a in args.kwonlyargs), "each annotated as where it comes from")
    fields = dataclasses.fields(m.NewProjectAccounts)
    check([f.name for f in fields] == ["provision_dir"] and fields[0].default is dataclasses.MISSING
          and fields[0].default_factory is dataclasses.MISSING and m.NewProjectAccounts.__dataclass_params__.frozen,
          "one frozen field, no default")
    check(sum(isinstance(n, ast.Return) for n in ast.walk(fn)) == 1
          and ast.unparse(fn.body[-1]) == "return NewProjectAccounts(provision_dir=provision_dir)", "one way out but a refusal")


def test_the_command_calls_it_where_the_steps_were() -> None:
    body = command_def().body
    at = next(i for i, s in enumerate(body) if isinstance(s, ast.Assign) and ast.unparse(s.targets[0]) == "new_project_accounts")
    check(ast.unparse(body[at - 1]) == "worktree_branch = new_project_preflight.worktree_branch", "right after P1's read-backs")
    call = body[at].value
    check(ast.unparse(call.func) == "_prepare_new_project_accounts" and not call.args
          and [(k.arg, ast.unparse(k.value)) for k in call.keywords] == [(n, n) for n in PARAMETERS + FROM_P0 + FROM_P1],
          "called by the launcher's name, with the command's own values")
    check(ast.unparse(body[at + 1]) == "provision_dir = new_project_accounts.provision_dir", "provision_dir read back")
    check(ast.unparse(body[at + 2]) == "stages.begin('database and board')", "then P3 begins, as before")


# --- behaviour -------------------------------------------------------------------------------------------------------


def test_the_accounts_in_order_with_their_arguments() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp), []
        values = inputs(tmp, world, said)
        got = run(world, values)
        check(isinstance(got, m.NewProjectAccounts), f"it goes on: {got!r}")
        check(world.names()[:5] == ["begin", "service-user", "peer-auth", "owner", "verify"],
              f"stage, service user, peer authentication, owner, then verification: {world.names()}")
        check(world.log[1] == ("service-user", "s371-board", values["runner"]), "the plan's service user, with the runner")
        check(world.log[2] == ("peer-auth", values["precheck_plan"], tmp / "source", values["runner"]), "peer auth for the plan and source")
        check(world.log[3] == ("owner", OWNER, tmp / "project", dict(runner=values["runner"], shell="fish", owner_home=tmp / "homes" / OWNER)),
              f"the owner and project directory, with the shell and home: {world.log[3]}")
        clis, kw = world.log[4][1:]
        check(clis is values["selected_role_clis"] and kw == dict(owner_user=OWNER, owner_home=tmp / "homes" / OWNER,
                                                                  runner=values["runner"], print_func=said.append),
              f"the gate's roles verified for the owner once the account exists: {kw}")


def test_what_is_said_about_the_account() -> None:
    cases = [
        (dict(created=True, shell_path="/bin/bash"), "fish", "switchyard: created user syrd371-agent with shell /bin/bash (fish unavailable); linger enabled"),
        (dict(created=True, shell_path="/usr/bin/fish"), "fish", "switchyard: created user syrd371-agent with shell fish; linger enabled"),
        (dict(created=True, shell_path=None), "/bin/zsh", "switchyard: created user syrd371-agent with shell /bin/zsh; linger enabled"),
        (dict(created=True, shell_path="/usr/bin/zsh"), "zsh", "switchyard: created user syrd371-agent with shell zsh; linger enabled"),
        (dict(created=False, shell_path=None), "fish", "switchyard: using existing user syrd371-agent (not modifying)"),
    ]
    for owner, shell, expected in cases:
        with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
            tmp = Path(tmp)
            world, said = World(tmp, **owner), []
            values = inputs(tmp, world, said, owner_shell=shell)
            run(world, values)
            check(said[0] == expected, f"{owner} with {shell}: {said[:1]}")
            check(world.log[3][3]["shell"] == shell, f"the account asked for with the chosen shell: {world.log[3]}")


def test_the_agy_credential() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp), []
        run(world, inputs(tmp, world, said))
        check(not {"agy-state", "agy-seed"} & set(world.names()), "no source, no agy step")
    for created in (True, False):
        with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
            tmp = Path(tmp)
            world, said = World(tmp, created=created, agy_state="absent"), []
            values = inputs(tmp, world, said, resolved_agy_credential_source="hostuser")
            run(world, values)
            check(("agy-state", OWNER, tmp / "homes") in world.log, "its state read for the owner")
            seeds = [e[1] for e in world.log if e[0] == "agy-seed"]
            check(len(seeds) == 1, f"absent: seeded once, whether or not the owner is new ({created}): {world.names()}")
            seed = seeds[0]
            check(seed == dict(owner_user=OWNER, source_user="hostuser", home_base=tmp / "homes", runner=values["runner"],
                               print_func=said.append), f"absent: seeded, whether or not the owner is new ({created}): {seed}")
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp, agy_state=t.AGY_CREDENTIAL_INSTALLED), []
        run(world, inputs(tmp, world, said, resolved_agy_credential_source="hostuser"))
        check("agy-seed" not in world.names() and "switchyard: agy credential already present for syrd371-agent; leaving it in place" in said,
              "installed: left in place")
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp, agy_state=t.AGY_CREDENTIAL_UNUSABLE), []
        got = run(world, inputs(tmp, world, said, resolved_agy_credential_source="hostuser"))
        token = tmp / "homes" / OWNER / t.AGY_CREDENTIAL_DIR_NAME / t.AGY_CREDENTIAL_TOKEN_NAME
        check(isinstance(got, SystemExit) and got.code == (
            f"switchyard: {token} exists but is not a 0600 regular file owned by {OWNER}, so agy could not read it; "
            "remove it and rerun, or clear capability_grants.agy_credential_source"), f"unusable: refused: {got!r}")
        check(world.names()[-1] == "agy-state" and not (tmp / "project" / ".switchyard" / "provision").exists(),
              "and nothing after it, not even the provisioning directory")


def test_the_fresh_branch() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp), []
        values = inputs(tmp, world, said, resolved_agy_credential_source="hostuser")
        run(world, values)
        check(world.names()[5:] == ["agy-state", "agy-seed", "onboarding", "artifact-write", "chown-files", "docs", "git-init", "policy"],
              f"onboarding, the artifact, ownership, documents, git, then the policy: {world.names()}")
        onboarding = next(e[1] for e in world.log if e[0] == "onboarding")
        expected = dict(project_name=values["resolved_project_name"], slug="s371", owner_user=OWNER, project_dir=tmp / "project",
                        artifact_path=values["artifact_path"], design_document=values["design_document"],
                        director_onboarding=values["director_onboarding"], include_designer=False)
        check(onboarding == expected, f"the onboarding files: {onboarding}")
        written = next(e[1] for e in world.log if e[0] == "artifact-write")
        identity = dict(implementer_roles="selected_implementer_roles", role_clis="selected_role_clis", role_models="selected_role_models",
                        role_efforts="selected_role_efforts", audit_roles="selected_audit_roles")
        check(all(written[k] is values[v] for k, v in identity.items()), "roles, models, efforts and audit roles, the very objects")
        check(written == dict(project_name=values["resolved_project_name"], slug="s371", owner_user=OWNER, project_dir=tmp / "project",
                              artifact_path=values["artifact_path"], design_document=values["design_document"], owner_shell="fish",
                              implementer_roles=values["selected_implementer_roles"], role_clis=values["selected_role_clis"],
                              role_models=values["selected_role_models"], role_efforts=values["selected_role_efforts"],
                              include_designer=False, include_audit=True, audit_roles=values["selected_audit_roles"],
                              agy_credential_source="hostuser", agy_credential_source_origin="host"),
              f"and every other field of the initial artifact: {written}")
        check(next(e[1] for e in world.log if e[0] == "chown-files") == dict(owner_user=OWNER, project_dir=tmp / "project", runner=values["runner"]),
              "the project's files given to the owner")
        check(next(e[1] for e in world.log if e[0] == "docs") == dict(source_repo=tmp / "source", project_dir=tmp / "project", owner_user=OWNER,
                                                                      runner=values["runner"], print_func=said.append), "the documents installed")


def test_the_designer_document() -> None:
    for include, present, chowned in ((True, True, True), (True, False, False), (False, True, False)):
        with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
            tmp = Path(tmp)
            world, said = World(tmp), []
            design = tmp / "DESIGN.md"
            if present:
                design.write_text("design\n", encoding="utf-8")
            values = inputs(tmp, world, said, include_designer=include, design_document=design)
            run(world, values)
            got = [e[1] for e in world.log if e[0] == "chown-design"]
            check(got == ([dict(owner_user=OWNER, path=design, runner=values["runner"])] if chowned else []),
                  f"designer {include}, document {present}: {got}")
            if chowned:
                check(world.names().index("chown-design") == world.names().index("chown-files") + 1, "right after the project's files")


def test_git_on_both_branches() -> None:
    for artifact in (None, "artifact"):
        for git_init, created, message, step in (
            (True, True, "switchyard: initialized git repository in {p} on branch trunk with an initial commit", "git-init"),
            (True, False, "switchyard: using existing git repository in {p} without modifying it", "git-init"),
            (False, None, "switchyard: skipped project git initialization for {p} (--no-git-init)", "git-require"),
        ):
            with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
                tmp = Path(tmp)
                world, said = World(tmp, git_created=bool(created)), []
                values = inputs(tmp, world, said, git_init=git_init, from_artifact=tmp / "a.json" if artifact else None)
                run(world, values)
                expect = message.format(p=tmp / "project")
                check(expect in said, f"{artifact} git_init={git_init} created={created}: {said}")
                entry = next(e[1] for e in world.log if e[0] == step)
                wanted = dict(owner_user=OWNER, project_dir=tmp / "project", runner=values["runner"])
                if step == "git-init":
                    wanted["branch"] = "trunk"
                check(entry == wanted and ({"git-init", "git-require"} - {step}).isdisjoint(world.names()),
                      f"only {step}, with its arguments: {entry}")
                if step == "git-require":
                    check(said.index(expect) >= 0 and world.names()[-2] == "git-require", "said, then required, just before the policy")
        with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
            tmp = Path(tmp)
            world, said = World(tmp, fail="git-require"), []
            got = run(world, inputs(tmp, world, said, git_init=False, from_artifact=tmp / "a.json" if artifact else None))
            check(isinstance(got, SystemExit) and world.names()[-1] == "git-require"
                  and not (tmp / "project" / ".switchyard" / "provision").exists(),
                  f"no repository without --git-init refuses, before the provisioning directory ({artifact}): {got!r}")


def test_the_artifact_branch() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp), []
        values = inputs(tmp, world, said, from_artifact=tmp / "a.json", include_designer=False)
        run(world, values)
        check(world.names()[5:] == ["artifact-load", "onboarding", "chown-files", "docs", "git-init", "policy"],
              f"reloaded, onboarding, ownership, documents, git, then the policy: {world.names()}")
        check(world.log[5] == ("artifact-load", values["artifact_path"]), "the artifact reloaded from its path")
        onboarding = next(e[1] for e in world.log if e[0] == "onboarding")
        check(onboarding["design_document"] is world.artifact.design_document and onboarding["include_designer"] is True
              and onboarding["artifact_path"] is values["artifact_path"], f"with the artifact's design document and designer: {onboarding}")
        check("artifact-write" not in world.names() and "chown-design" not in world.names(), "no initial artifact, no designer chown")


def test_the_provisioning_directory_and_the_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp), []
        values = inputs(tmp, world, said)
        got = run(world, values)
        default = (tmp / "project" / ".switchyard" / "provision").resolve(strict=False)
        check(got.provision_dir == default and default.is_dir(), f"the project's provisioning directory, created: {got.provision_dir}")
        path, payload = world.log[-1][1:]
        check(world.names()[-1] == "policy" and path == default / "desktop-policy.json" and payload is values["selected_desktop_policy"],
              "the very desktop policy written there, last")
        check(isinstance(judged(setattr, got, "provision_dir", tmp), dataclasses.FrozenInstanceError), "frozen")
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp), []
        got = run(world, inputs(tmp, world, said, output_dir=tmp / "a" / ".." / "out"))
        check(got.provision_dir == (tmp / "out").resolve(strict=False) and (tmp / "out").is_dir(), "the caller's, resolved")


def test_every_refusal_stops_what_follows() -> None:
    order = ["begin", "service-user", "peer-auth", "owner", "verify", "agy-state", "agy-seed", "onboarding", "artifact-write",
             "chown-files", "docs", "git-init", "policy"]
    for step in order[1:]:
        with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
            tmp = Path(tmp)
            world, said = World(tmp, fail=step), []
            got = run(world, inputs(tmp, world, said, resolved_agy_credential_source="hostuser"))
            check(isinstance(got, SystemExit) and got.code == f"refused at {step}" and world.names() == order[:order.index(step) + 1],
                  f"refused at {step}, and nothing after it: {got!r} {world.names()}")


# --- the caller ------------------------------------------------------------------------------------------------------


class Fence(Exception):
    """P3 reached: stop there."""


def command_locals(exc: BaseException) -> dict:
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code is t.switchyard_new_command.__code__:
            return dict(tb.tb_frame.f_locals)
        tb = tb.tb_next
    raise AssertionError("the command's frame is not on the way to the fence")


def earlier(tmp: Path, log: list) -> tuple:
    p0 = m.NewProjectChoices(**{f.name: object() for f in dataclasses.fields(m.NewProjectChoices)})
    p0 = dataclasses.replace(p0, stages=Stages(log), owner_user=OWNER, project_dir=tmp / "project", resolved_slug="s371",
                             resolved_agy_credential_source="", include_designer=False, owner_shell="fish",
                             artifact_path=tmp / "project" / ".switchyard" / "s371.project.json",
                             design_document=tmp / "project" / "DESIGN.md", runner=lambda *a, **k: None)
    p1 = m.NewProjectPreflight(effective_source_repo=tmp / "source", precheck_plan=SimpleNamespace(service_user="s371-board"),
                               selected_role_clis=(("main", "codex"),), worktree_branch="trunk")
    return p0, p1


def p3_fence(seen: list) -> dict:
    def new_project(slug, **kw):
        seen.append(("new-project", slug, kw))
        raise Fence()
    return dict(new_project_command=new_project, _prepare_first_run_auth_worktrees=refuse("the auth worktrees"),
                _register_switchyard_project=refuse("the registration"))


def test_the_command_hands_p2_its_values_and_reads_back_provision_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1 = earlier(tmp, log)
        answer = m.NewProjectAccounts(provision_dir=object())
        handed: list = []
        seen: list = []
        given = {name: object() for name in PARAMETERS}
        given.update(home_base=tmp / "homes", from_artifact=None)
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                     _prepare_new_project_accounts=lambda **kw: handed.append(kw) or answer, **p3_fence(seen)):
            got = judged(t.switchyard_new_command, **given)
        check(isinstance(got, Fence), f"stopped at P3: {got!r}")
        check(len(handed) == 1 and tuple(handed[0]) == PARAMETERS + FROM_P0 + FROM_P1, f"P2 called once, with its 28 values: {handed}")
        wrong = ([n for n in PARAMETERS if handed[0][n] is not given[n]] + [n for n in FROM_P0 if handed[0][n] is not getattr(p0, n)]
                 + [n for n in FROM_P1 if handed[0][n] is not getattr(p1, n)])
        check(wrong == [], f"each the very value: the command's own, P0's or P1's: {wrong}")
        local = command_locals(got)
        check(local["provision_dir"] is answer.provision_dir, "provision_dir is the command's, the very object")
        check(local["first_run_runner"] is p0.first_run_runner and local["runner"] is p0.runner, "P0's two runners untouched")
        check(log == [("begin", "database and board")] and seen[0][2]["output_dir"] is answer.provision_dir,
              f"then P3 begins, provisioning into it: {log}")


def test_the_real_phase_between_p1_and_p3() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd371.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1 = earlier(tmp, log)
        world = World(tmp)
        world.log = log
        seen: list = []
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                     **world.seams(), **p3_fence(seen)):
            got = judged(t.switchyard_new_command, source_repo=tmp / "source", home_base=tmp / "homes", config_dir=tmp / "config",
                         registry_dir=tmp / "registry", input_func=refuse("input"), print_func=lambda line: None)
        check(isinstance(got, Fence), f"stopped at P3: {got!r}")
        local = command_locals(got)
        expected = (tmp / "project" / ".switchyard" / "provision").resolve(strict=False)
        check(local["provision_dir"] == expected and seen[0][2]["output_dir"] == expected, "the directory P2 made is the one P3 uses")
        check(world.log[1] == ("service-user", "s371-board", p0.runner) and [e[0] for e in log][-2:] == ["policy", "begin"]
              and log[-1] == ("begin", "database and board"), f"P2 with P1's plan and P0's runner, then P3's stage: {[e[0] for e in log]}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_and_nothing_bound_read_through_the_launcher")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"new_project_accounts_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
