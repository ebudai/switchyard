#!/usr/bin/env python3
"""SYRD-358: `switchyard set-role-runtime`, against the launcher it came out of.

`set_project_role_runtime_command` and its `_role_named` moved into
`scripts/project_role_runtime.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top, and the
  command still imports `scripts.role_runtime` inside itself, so a patch of
  `role_runtime.switch_role_runtime` still reaches it.
- **One set of objects.** The launcher re-exports both names, the very same
  objects whichever module is imported first. The `runner`, `input_func` and
  `print_func` defaults are `subprocess.run`, `input` and `print` themselves.
- **Seams (rule 24).** Every launcher facility it uses is read from the launcher
  when it runs: the role lookup, the role's CLI, the owner's catalog prefix, the
  runtime and model fields, the catalog and the terminal selector. Nothing it
  binds is read through the launcher (rule 27).
- **The behaviour is unchanged:**
  - the runtime named, or chosen at a terminal, and refused without one;
  - the model checked against the OWNER's catalog: an explicit one refused when
    unavailable; `""` dropping it; `None` keeping the configured model unless
    the runtime changes or the owner does not offer it;
  - a changed runtime has its model chosen again at a terminal, or reset without
    one;
  - the same runtime with an unavailable model is repaired at a terminal and
    refused without one;
  - the switch delegated with exactly its arguments;
  - a dry run, a switch and a forced interruption each reported.

Every facility is this test's own recording fake, installed before the command
runs. No provider, model catalog, board, pane, tmux session or service is
touched.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
MOVED = ("_role_named", "set_project_role_runtime_command")
OWN = ("subprocess", "sys", "Path", "Any", "Callable")
SEAMS = {"_role_named": 1, "_role_cli_name": 1, "_owner_catalog_args": 1, "_runtime_field": 1, "_model_field": 2,
         "runtime_catalog": 4, "terminal_select": 3}
CONFIG_PATH = Path("/nonexistent/syrd358/p358.json")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


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


def attempt(action):
    """The command's answer, or the refusal it raised, as a value to assert on."""
    try:
        return action()
    except SystemExit as exc:
        return f"refused: {exc}"


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.project_role_runtime as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.project_role_runtime", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_role_runtime")):
        result = python("import importlib, subprocess, builtins; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_role_runtime as m; "
                        "k = m.set_project_role_runtime_command.__kwdefaults__; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"all(getattr(t, n) is getattr(m, n) for n in {OWN!r}), "
                        "k['runner'] is subprocess.run and k['input_func'] is builtins.input and k['print_func'] is builtins.print)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_local_import_and_the_dispatch() -> None:
    module = ast.parse((ROOT / "scripts" / "project_role_runtime.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    check(list(functions) == list(MOVED), f"both, in baseline order: {list(functions)}")
    command = functions["set_project_role_runtime_command"]
    through: dict[str, int] = {}
    for n in ast.walk(module):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
            through[n.attr] = through.get(n.attr, 0) + 1
    bare = sorted({n.id for n in ast.walk(module) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in SEAMS})
    check(through == SEAMS and not bare, f"13 reads of seven launcher names, none bare: {through} {bare}")
    imports = [ast.unparse(n) for n in command.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(imports == ["from scripts import team_launcher as launcher", "from scripts import role_runtime"]
          and not [n for n in ast.walk(command) if isinstance(n, ast.Attribute) and n.attr == "role_runtime"],
          f"role_runtime is still the command's own import, never the launcher's: {imports}")
    check(not [n for n in ast.walk(functions["_role_named"]) if isinstance(n, (ast.Import, ast.ImportFrom))],
          "the lookup reads nothing of the launcher's")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    check(not {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)} & set(MOVED)
          and "_owner_catalog_args" in {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)}
          and len([n for n in ast.walk(launcher) if isinstance(n, ast.Call) and ast.unparse(n.func) == "set_project_role_runtime_command"]) == 1,
          "the launcher defines neither, keeps the shared catalog prefix, and dispatches by its own name")


# --- the command ----------------------------------------------------------------------------------------------------


class Result:
    def __init__(self, *, changed: bool = True, forced: bool = False, reason: str = "") -> None:
        self.configured_changed, self.forced, self.reason = changed, forced, reason
        self.role, self.previous_runtime, self.runtime = "audit", "codex", "claude"

    def describe(self) -> str:
        return "SWITCHED"


class Switch:
    """The command's launcher facilities and the delegated switch, answering from this test's objects, into one log."""

    def __init__(self, *, cli: str = "codex", model: str = "", offered: tuple = ("m-one", "m-two"), owner: str = "syrd358-owner",
                 picks: tuple = ("claude", "m-picked"), result: Result | None = None, error: Exception | None = None) -> None:
        self.role = SimpleNamespace(role="audit", cli=cli, model=model)
        self.config = SimpleNamespace(project="p358", roles=[SimpleNamespace(role="main", cli="codex", model=""), self.role])
        self.offered, self.owner, self.picks = offered, owner, list(picks)
        self.result, self.error = result or Result(), error
        self.log: list[tuple] = []
        self.said: list[str] = []

    def names(self) -> dict:
        L = self.log

        def select_one(field, *args, input_func, print_func):
            L.append(("select", field, args))
            return self.picks.pop(0)

        def absent(catalog, model):
            L.append(("absent?", catalog, model))
            if model in self.offered:
                return None
            return SimpleNamespace(choices=[SimpleNamespace(value=v) for v in self.offered])

        catalog = SimpleNamespace(owner_model_catalog=lambda runtime, *, runner, owner_args: L.append(("catalog", runtime, owner_args)) or f"CAT-{runtime}",
                                  model_absent_from=absent)
        return dict(
            _role_cli_name=lambda role: L.append(("cli?", role.role)) or role.cli,
            _owner_catalog_args=lambda config: L.append(("owner?", config.project)) or (self.owner, ("PREFIX", self.owner)),
            _runtime_field=lambda role, *, default: L.append(("runtime-field", role, default)) or "RUNTIME-FIELD",
            _model_field=lambda role, *, runner, owner_args, print_func: L.append(("model-field", role, owner_args)) or "MODEL-FIELD",
            runtime_catalog=catalog,
            terminal_select=SimpleNamespace(select_one=select_one),
        )

    def run(self, **kwargs):
        from scripts import project_role_runtime as m, role_runtime, team_launcher

        def switch(config, **kw):
            self.log.append(("switch", config, kw))
            if self.error:
                raise self.error
            return self.result

        kwargs.setdefault("role_name", "audit")
        with patched(team_launcher, **self.names()), patched(role_runtime, switch_role_runtime=switch):
            return m.set_project_role_runtime_command(self.config, config_path=CONFIG_PATH, runner="RUNNER",
                                                      print_func=self.said.append, input_func="INPUT", **kwargs)

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]

    def switched(self) -> dict:
        return next(e for e in self.log if e[0] == "switch")[2]


def test_the_runtime_is_named_chosen_or_refused() -> None:
    fx = Switch()
    try:
        fx.run(interactive=False); raised = None
    except SystemExit as exc:
        raised = str(exc)
    check(raised == "switchyard: set-role-runtime needs --cli when there is no terminal to choose at" and "switch" not in fx.kinds(),
          "no runtime and no terminal: refused before anything changes")
    fx = Switch(picks=("claude", "m-one"))
    fx.run(interactive=True, model="")
    check(fx.kinds()[:4] == ["cli?", "owner?", "runtime-field", "select"] and fx.log[2][1:] == ("audit", "codex")
          and fx.log[3][1:] == ("RUNTIME-FIELD", ()) and fx.switched()["runtime"] == "claude",
          f"at a terminal the operator chooses, the current runtime offered first: {fx.log[:4]}")
    fx = Switch(cli="")
    fx.run(interactive=True, model="")
    check(fx.log[2][2] == "codex", "a role with no current runtime is offered codex first")
    fx = Switch(cli="claude", picks=("claude",))
    fx.run(interactive=True, model="")
    check(fx.log[2][2] == "claude", "a role on claude is offered claude first")
    fx = Switch()
    with patched(sys, stdin=SimpleNamespace(isatty=lambda: False)):
        try:
            fx.run(); raised = None
        except SystemExit:
            raised = "refused"
    check(raised == "refused", "with no preference stated and no terminal on stdin: refused")
    fx = Switch(picks=("claude",))
    with patched(sys, stdin=SimpleNamespace(isatty=lambda: True)):
        answer = attempt(lambda: fx.run(model=""))
    check(answer == 0 and "select" in fx.kinds() and fx.switched()["runtime"] == "claude", "with no preference stated, a terminal on stdin asks")


def test_an_explicit_model_is_checked_against_the_owners_catalog() -> None:
    fx = Switch()
    answer = attempt(lambda: fx.run(runtime="claude", model=" m-two ", interactive=False))
    check(answer == 0 and fx.log[2] == ("catalog", "claude", ("PREFIX", "syrd358-owner")) and fx.log[3] == ("absent?", "CAT-claude", "m-two")
          and fx.switched()["model"] == "m-two", f"stripped, checked against the owner's own catalog, and used: {fx.log}")
    fx = Switch()
    try:
        fx.run(runtime="claude", model="m-zzz", interactive=True); raised = None
    except SystemExit as exc:
        raised = str(exc)
    check(raised == "switchyard: claude on syrd358-owner does not offer 'm-zzz'; it offers: m-one, m-two. Nothing was changed."
          and "switch" not in fx.kinds() and "select" not in fx.kinds(), "an unavailable explicit model is refused, never replaced")
    fx = Switch(owner="")
    try:
        fx.run(runtime="claude", model="m-zzz", interactive=False)
    except SystemExit as exc:
        raised = str(exc)
    check("claude on the project account does not offer" in raised, "no owner: named as the project account")
    fx = Switch()
    fx.run(runtime="claude", model="", interactive=True)
    check("catalog" not in fx.kinds() and fx.switched()["model"] == "", "an empty model drops it, with nothing to check")


def test_a_changed_runtime_chooses_its_model_again() -> None:
    fx = Switch(model="gpt-old", picks=("m-picked",))
    fx.run(runtime="claude", interactive=True)
    check(fx.said[0] == "switchyard: audit's model gpt-old belongs to codex; claude advertises its own"
          and fx.log[-2] == ("select", "MODEL-FIELD", ({"runtime": "claude"},)) and fx.switched()["model"] == "m-picked"
          and ("model-field", "audit", ("PREFIX", "syrd358-owner")) in fx.log,
          f"said, then chosen from the owner's list for the new runtime: {fx.log}")
    fx = Switch(model="gpt-old")
    fx.run(runtime="claude", interactive=False)
    check(fx.switched()["model"] == "" and "select" not in fx.kinds(), "nobody to ask: the new runtime's own default")
    fx = Switch(model="")
    fx.run(runtime="claude", interactive=False)
    check(fx.switched()["model"] is None and fx.said == ["SWITCHED"], "no configured model and nobody to ask: left alone")
    fx = Switch(model="", picks=("m-picked",))
    fx.run(runtime="claude", interactive=True)
    check(fx.switched()["model"] == "m-picked" and not fx.said[0].startswith("switchyard: audit's model"),
          "no configured model: nothing said about one, and still chosen at a terminal")


def test_the_same_runtime_with_a_model_the_owner_does_not_offer() -> None:
    fx = Switch(model="m-one")
    fx.run(runtime="codex", interactive=False)
    check(fx.switched()["model"] is None and fx.kinds().count("absent?") == 1, "an offered model is kept as configured")
    fx = Switch(model="m-gone", picks=("m-two",))
    answer = attempt(lambda: fx.run(runtime="codex", interactive=True))
    check(answer == 0 and fx.said[0] == ("switchyard: codex on syrd358-owner does not offer audit's configured model 'm-gone', so the provider "
                         "would ignore it and run something else. That account offers: m-one, m-two.")
          and fx.log[-2] == ("select", "MODEL-FIELD", ({"runtime": "codex"},)) and fx.switched()["model"] == "m-two",
          f"said, then repaired at a terminal from the owner's list: {fx.said}")
    fx = Switch(model="m-gone")
    try:
        fx.run(runtime="codex", interactive=False); raised = None
    except SystemExit as exc:
        raised = str(exc)
    check(raised == ("switchyard: nothing was changed. Re-run naming the model, for example `switchyard set-role-runtime "
                     "p358 audit --cli codex --model m-one`, or pass `--model ''` to take codex's own default.")
          and "switch" not in fx.kinds(), f"refused without a terminal, naming the remedy: {raised}")


def test_the_switch_is_delegated_exactly_and_reported() -> None:
    fx = Switch(model="")
    code = fx.run(runtime="claude", model="m-one", force=True, reason="stuck", dry_run=False,
                  pane_state_dir=Path("/nonexistent/syrd358/state"), interactive=False)
    kw = fx.switched()
    check(code == 0 and next(e for e in fx.log if e[0] == "switch")[1] is fx.config and kw == dict(
        config_path=CONFIG_PATH, role_name="audit", runtime="claude", model="m-one", force=True, reason="stuck", dry_run=False,
        pane_state_dir=Path("/nonexistent/syrd358/state"), runner="RUNNER", print_func=fx.said.append),
          f"every argument handed on as given: {kw}")
    check(fx.said == ["SWITCHED"], f"the switch described: {fx.said}")
    fx = Switch(result=Result(forced=True, reason="stuck"))
    fx.run(runtime="claude", model="", interactive=False)
    check(fx.said == ["SWITCHED", "switchyard: forced past a busy role: stuck"], f"a forced interruption said: {fx.said}")
    fx = Switch(result=Result(forced=True, reason=""))
    fx.run(runtime="claude", model="", interactive=False)
    check(fx.said == ["SWITCHED"], "forced without a reason: nothing more said")
    fx = Switch(result=Result(changed=True))
    check(fx.run(runtime="claude", model="", dry_run=True, interactive=False) == 0
          and fx.said == ["switchyard: would move audit from codex to claude; every check passed and nothing was changed"],
          f"a dry run that would change something says so and nothing else: {fx.said}")
    fx = Switch(result=Result(changed=False))
    fx.run(runtime="claude", model="", dry_run=True, interactive=False)
    check(fx.said == ["SWITCHED"], "a dry run that would change nothing is described as the switch describes it")


def test_a_missing_role_and_errors() -> None:
    fx = Switch(cli="claude")
    fx.config.roles.append(SimpleNamespace(role="audit", cli="agy", model=""))
    fx.run(runtime="claude", model="", interactive=False)
    check(("cli?", "audit") in fx.log and "select" not in fx.kinds() and fx.said == ["SWITCHED"]
          and next(e for e in fx.log if e[0] == "cli?") and fx.role.cli == "claude",
          "the first role of that name is the one looked up")
    fx2 = Switch(cli="claude", picks=("claude",))
    fx2.config.roles.append(SimpleNamespace(role="audit", cli="agy", model=""))
    fx2.run(interactive=True, model="")
    check(fx2.log[2][2] == "claude", f"its runtime, not a later duplicate's, is the one offered: {fx2.log[2]}")
    fx = Switch()
    fx.run(role_name="ghost", runtime="claude", model="", interactive=True)
    check("cli?" not in fx.kinds() and fx.switched()["role_name"] == "ghost",
          "a role not configured has no current runtime and is handed to the switch, which decides")
    boom = OSError("syrd358: the switch raised")
    fx = Switch(error=boom)
    try:
        fx.run(runtime="claude", model="", interactive=False); raised = None
    except OSError as exc:
        raised = exc
    check(raised is boom and fx.said == [], "not caught, nothing reported")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_the_local_import_and_the_dispatch")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"project_role_runtime_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
