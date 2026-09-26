#!/usr/bin/env python3
"""SYRD-308: the role-identity cutover's boundary with the launcher it came out of.

The cutover moved into `scripts/role_identity_cutover.py`, and its default
release ref into the leaf `scripts/release_refs.py`, both unchanged. This pins
what makes that safe:

- **No cycle.** The leaf imports nothing; the cutover imports only the leaf;
  neither imports the launcher at its top.
- Every name the launcher's upgrade, finish-upgrade and recovery code and the
  suites reach as `team_launcher.<name>` is still there and is the very same
  object, whichever module is imported first.
- **The default ref is one object** for the cutover command and the release
  functions that stayed in the launcher.
- **The patched names are still reached.** The suites patch
  `cutover_role_identities_command` and `_interrupted_provider_state_roles`
  on the launcher; the launcher still calls each at exactly its one baseline
  site, by its own name. The generic seams -- `process_uid`,
  `reconnect_presentation`, `_start_role_sessions_without_a_window` -- are
  read only through the launcher, and none of them moved here.
- The identities a cutover targets come from the launcher's account naming
  and home lookup as they are when it runs.

Nothing here probes a process, starts a session or touches an account.
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

EXPORTED = (
    'canonical_role_identities',
    'cutover_role_identities_command',
    '_finish_interrupted_provider_state',
    '_interrupted_provider_state_roles',
    'repatriate_role_runtime_state',
    'revert_incomplete_role_account_cutover',
    'role_account_cutover',
    'running_role_identities',
    '_worktree_ownership',
)
PATCHED_MOVED = ('_interrupted_provider_state_roles', 'cutover_role_identities_command')
#: The names a function imports the launcher under at call time. The cutover
#: command has its own `launcher` parameter (an injected session starter), so
#: it imports the module as `team_launcher` instead (SYRD-308).
LAUNCHER_ALIASES = ("launcher", "team_launcher")
GENERIC_SEAMS = ('PROC_ROOT', '_proc_effective_uid', '_start_role_sessions_without_a_window',
                 'presentation_is_attached', 'process_uid', 'reconnect_presentation')


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
    check(loaded_after("scripts.release_refs") == [], "the release ref is a leaf")
    check(loaded_after("scripts.role_identity_cutover") == ["scripts.release_refs"],
          "and the cutover loads only it, never the launcher")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.role_identity_cutover", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.role_identity_cutover")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.role_identity_cutover as c, scripts.release_refs as r; "
            f"print(all(getattr(t, n) is getattr(c, n) for n in {EXPORTED!r}), "
            "t.DEFAULT_TENANT_RELEASE_DEPLOY_REF is r.DEFAULT_TENANT_RELEASE_DEPLOY_REF)"
        )
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_default_ref_is_one_object() -> None:
    from scripts import release_refs, role_identity_cutover, team_launcher

    ref = release_refs.DEFAULT_TENANT_RELEASE_DEPLOY_REF
    cutover = inspect.signature(role_identity_cutover.cutover_role_identities_command).parameters["deploy_ref"].default
    stayed = [
        inspect.signature(fn).parameters["deploy_ref"].default
        for fn in vars(team_launcher).values()
        if inspect.isfunction(fn) and fn.__module__ == "scripts.team_launcher"
        and "deploy_ref" in inspect.signature(fn).parameters
        and inspect.signature(fn).parameters["deploy_ref"].default is not inspect.Parameter.empty
        and isinstance(inspect.signature(fn).parameters["deploy_ref"].default, str)
    ]
    check(cutover is ref, "the cutover command defaults to the leaf's ref")
    check(len(stayed) == 2 and all(default is ref for default in stayed),
          f"and so do the two release functions that stayed in the launcher: {len(stayed)}")


def test_the_patched_and_generic_seams_are_reached() -> None:
    tree = ast.parse((ROOT / "scripts" / "role_identity_cutover.py").read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    check(not defined & set(GENERIC_SEAMS), f"no generic seam moved here: {sorted(defined & set(GENERIC_SEAMS))}")
    bare = sorted({n.id for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in GENERIC_SEAMS + PATCHED_MOVED})
    check(bare == [], f"none of them is read past the launcher here: {bare}")
    through = sorted({n.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                      and n.value.id in LAUNCHER_ALIASES and n.attr in GENERIC_SEAMS})
    check(through == ["_start_role_sessions_without_a_window", "presentation_is_attached", "process_uid",
                     "reconnect_presentation"],
          f"the ones it uses, it reads through the launcher: {through}")
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    for name in PATCHED_MOVED:
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) == 1 and isinstance(calls[0].func, ast.Name),
              f"the launcher calls {name} at its one baseline site, by its own patchable name")


def test_no_call_time_import_shadows_what_a_function_binds() -> None:
    """The cutover's own `launcher` parameter must stay the caller's session starter.

    The first cut imported the launcher module under that very name inside the
    function, so `launcher or (...)` became the module and the restart called it.
    """
    tree = ast.parse((ROOT / "scripts" / "role_identity_cutover.py").read_text(encoding="utf-8"))
    shadowed = []
    for fn in [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        aliases = {a.asname or a.name for n in fn.body if isinstance(n, ast.ImportFrom) and n.module == "scripts"
                   for a in n.names if a.name == "team_launcher"}
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
        stores = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        if aliases & (params | stores):
            shadowed.append(fn.name)
    check(shadowed == [], f"no call-time import rebinds a parameter or local: {shadowed}")
    from scripts import role_identity_cutover

    check("launcher" in inspect.signature(role_identity_cutover.cutover_role_identities_command).parameters,
          "and the cutover still takes the caller's `launcher`")


def test_the_target_identities_come_from_the_launcher_when_asked() -> None:
    from scripts import role_identity_cutover, team_launcher

    config = SimpleNamespace(project="p308", run_as_user="syrd-308-owner",
                             roles=[SimpleNamespace(role="main", run_as_user="", workdir="/nonexistent/w")])
    saved = (team_launcher.role_account_name, team_launcher.home_dir_for_user)
    team_launcher.role_account_name = lambda project, role: f"syrd-308-{project}-{role}"
    team_launcher.home_dir_for_user = lambda account: Path(f"/nonexistent/home/{account}")
    try:
        identities = role_identity_cutover.canonical_role_identities(config)
    finally:
        team_launcher.role_account_name, team_launcher.home_dir_for_user = saved
    check(identities == {"main": {"account": "syrd-308-p308-main",
                                  "home": "/nonexistent/home/syrd-308-p308-main",
                                  "worktree": "/nonexistent/w"}},
          f"named and homed by the launcher's functions as patched: {identities!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"role_identity_cutover_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
