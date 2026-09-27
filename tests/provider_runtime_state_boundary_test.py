#!/usr/bin/env python3
"""SYRD-306: provider-state and runtime-registration reads, and the privilege-drop leaf.

Provider-state generation and runtime-registration reads moved into
`scripts/provider_runtime_state.py`, and the privilege drop they default to
into the leaf `scripts/account_drop.py`, all unchanged. This pins what makes
that safe:

- **No cycle.** The leaf imports nothing of Switchyard's; the state module
  imports only the leaf; neither imports the launcher at its top.
- Every name the launcher's launch, recovery, resume and cutover code, the
  modules that read through the launcher (`presentation_controller`,
  `project_status`) and the suites reach as `team_launcher.<name>` is still
  there and is the very same object, whichever module is imported first.
- **Def-time identities hold.** The writers' `drop` default is the leaf's
  one `_drop_to_account`, and the registration wait's timeout default is
  the same object as the launcher's recovery check defaults to.
- **The launcher's seams are still reached.** `_read_json_object` and
  `FIRST_RUN_SETUP_CLIS` are read through the launcher when a function
  runs, and the recovery check still calls `await_runtime_registration` by
  the launcher's own, patchable, name.

Nothing here writes provider state, drops privileges or reads a board.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED_STATE = (
    'await_runtime_registration',
    'provider_state_generation',
    'provider_state_store_problem',
    '_provider_state_record_path',
    'read_runtime_assignment_details',
    'record_provider_state_generation',
    'recorded_provider_state_generation',
    'RUNTIME_REGISTRATION_POLL_SECONDS',
    'RUNTIME_REGISTRATION_TIMEOUT_SECONDS',
    'RuntimeRegistrationWait',
    'unreadable_provider_state_roles',
)
EXPORTED_DROP = ('_drop_to_account', '_run_as_account')
LAUNCHER_SEAMS = ('FIRST_RUN_SETUP_CLIS', '_read_json_object', 'await_runtime_registration')


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def loaded_after(module: str) -> list[str]:
    result = python(
        f"import sys, {module}; "
        f"print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != {module!r}))"
    )
    check(result.returncode == 0, f"{module} imports on its own: {result.stderr[-600:]}")
    return eval(result.stdout.strip())


def test_the_dependencies_point_one_way() -> None:
    check(loaded_after("scripts.account_drop") == [], "the privilege drop is a leaf")
    check(loaded_after("scripts.provider_runtime_state") == ["scripts.account_drop"],
          "and the state module loads only it, never the launcher")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.provider_runtime_state", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.provider_runtime_state")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.provider_runtime_state as s, scripts.account_drop as d; "
            f"print(all(getattr(t, n) is getattr(s, n) for n in {EXPORTED_STATE!r}), "
            f"all(getattr(t, n) is getattr(d, n) for n in {EXPORTED_DROP!r}))"
        )
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_def_time_defaults_are_one_object() -> None:
    from scripts import account_drop, provider_runtime_state, team_launcher

    drop = account_drop._drop_to_account
    for function in (provider_runtime_state.record_provider_state_generation,
                     provider_runtime_state.provider_state_store_problem,
                     account_drop._run_as_account):
        default = inspect.signature(function).parameters["drop"].default
        check(default is drop, f"{function.__name__} drops privileges through the leaf's one function")
    wait = inspect.signature(provider_runtime_state.await_runtime_registration).parameters["timeout_seconds"].default
    recovery = inspect.signature(team_launcher.recovery_readiness_problems).parameters["runtime_wait_seconds"].default
    check(wait is recovery is team_launcher.RUNTIME_REGISTRATION_TIMEOUT_SECONDS,
          "the registration wait and the launcher's recovery check default to the same timeout object")


def test_the_launcher_seams_are_reached() -> None:
    tree = ast.parse((ROOT / "scripts" / "provider_runtime_state.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in LAUNCHER_SEAMS})
    through = sorted({n.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                      and n.value.id == "launcher" and n.attr in LAUNCHER_SEAMS})
    check(bare == [], f"no launcher seam is read past the launcher: {bare}")
    check(through == ["FIRST_RUN_SETUP_CLIS", "_read_json_object"], f"and the shared ones are read through it: {through}")
    # The recovery check moved to scripts/recovery_readiness.py (SYRD-365), where
    # the launcher's own name is read as `launcher.await_runtime_registration`;
    # a bare call there would read that module's global and miss the patch.
    calls = []
    for name in ("team_launcher.py", "recovery_readiness.py"):
        if not (ROOT / "scripts" / name).exists():
            continue
        tree = ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
        calls += [(name, n) for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "await_runtime_registration"]
    check(len(calls) == 1 and (
              (calls[0][0] == "team_launcher.py" and isinstance(calls[0][1].func, ast.Name))
              or (calls[0][0] == "recovery_readiness.py" and isinstance(calls[0][1].func, ast.Attribute)
                  and isinstance(calls[0][1].func.value, ast.Name) and calls[0][1].func.value.id == "launcher")),
          "the recovery check calls the wait by the launcher's own (patched) name")


def test_a_recorded_generation_is_read_by_the_launchers_reader_when_it_runs() -> None:
    from scripts import provider_runtime_state, team_launcher

    asked: list[Path] = []
    saved = (team_launcher._read_json_object, team_launcher.role_session_dir)
    team_launcher.role_session_dir = lambda config, role: Path("/nonexistent/syrd-306/sessions")
    team_launcher._read_json_object = lambda path: asked.append(path) or {"generation": "syrd-306-generation"}
    try:
        generation = provider_runtime_state.recorded_provider_state_generation(
            SimpleNamespace(project="p306"), SimpleNamespace(role="main"))
    finally:
        team_launcher._read_json_object, team_launcher.role_session_dir = saved
    check(generation == "syrd-306-generation", f"the stamp came from the launcher's reader: {generation!r}")
    check(asked == [Path("/nonexistent/syrd-306/sessions/main.provider-state.json")],
          f"at the role's record under the launcher's session directory: {asked!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"provider_runtime_state_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
