#!/usr/bin/env python3
"""SYRD-313: role pane entry, provider resume and the environment leaf, against the launcher they came out of.

Pane entry moved into `scripts/role_pane_entry.py`, provider resume stores,
Hermes homes and resume verification into `scripts/provider_resume.py`, and
`_env_first` with `DEFAULT_PANE_STATE_DIR` into the leaf
`scripts/launcher_env.py`, all unchanged. This pins what makes that safe:

- **The imports point one way:** the leaf imports nothing of Switchyard's; the
  resume module imports nothing of Switchyard's; pane entry imports only those
  two; none imports the launcher at its top.
- Every name the launcher and the modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **Rule 6.** `DEFAULT_PANE_STATE_DIR` is the default argument of the four
  entry points and its only users: one object, shared with the launcher's name.
- **Rule 20.** The rebound timings and roots stay in the launcher and are
  read there when a function runs; rebinding `DEFAULT_PANE_STATE_DIR` on the
  launcher still reaches `session_paths`.
- **The patched entry points are still reached** by the launcher's name.

Nothing here starts tmux, a provider or a process; paths are temporary.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = {
    "scripts.launcher_env": ('DEFAULT_PANE_STATE_DIR', '_env_first'),
    "scripts.provider_resume": (
        '_claude_project_dir_for_workdir', 'clear_unverified_resume_for_role', 'CODEX_SESSIONS_DIR_NAME',
        'hermes_home_for_role', 'HERMES_PRIVATE_HOME_ENTRIES', 'HERMES_SHARED_HOME_ENTRIES',
        '_home_from_session_dir', 'prepare_hermes_home_for_role', '_resume_launch_status',
        'RESUME_LAUNCH_TIMEOUT', 'RESUME_LAUNCH_VERIFIED', '_resume_preflight_allows_attempt', '_uses_hermes',
    ),
    "scripts.role_pane_entry": (
        'attach_role_to_slot', 'ensure_visible_role_session_for_viewer', 'run_detached_role', 'run_role_pane',
        'tmux_new_session_args',
    ),
}
REBOUND = ("AGY_CONVERSATION_ROOT", "RESUME_STARTUP_TIMEOUT_SECONDS", "RESUME_STARTUP_POLL_SECONDS",
           "DETACHED_SESSION_STABILITY_SECONDS")
PATCHED_ENTRY_POINTS = ("run_role_pane", "run_detached_role", "ensure_visible_role_session_for_viewer")
#: Measured on the SYRD-342 baseline: each entry point's call sites. SYRD-342
#: moved launch_project's P7+P8, holding one detached start and two viewer
#: starts, to launch_phases, which calls them through the launcher; the totals
#: are unchanged.
ENTRY_POINT_CALLS = {"run_role_pane": 1, "run_detached_role": 2, "ensure_visible_role_session_for_viewer": 3}


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


def test_the_imports_point_one_way() -> None:
    check(loaded_after("scripts.launcher_env") == [], "the environment leaf loads nothing of Switchyard's")
    check(loaded_after("scripts.provider_resume") == [], "nor does provider resume")
    check(loaded_after("scripts.role_pane_entry") == ["scripts.launcher_env", "scripts.provider_resume"],
          "and pane entry loads only those two, never the launcher")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for module, names in EXPORTED.items():
        for order in ((module, "scripts.team_launcher"), ("scripts.team_launcher", module)):
            result = python(
                "import importlib; "
                f"[importlib.import_module(m) for m in {order!r}]; "
                f"import scripts.team_launcher as t, {module} as m; "
                f"print(all(getattr(t, n) is getattr(m, n) for n in {names!r}))"
            )
            check(result.stdout.strip() == "True",
                  f"{' then '.join(order)}: {module}'s names are the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_pane_state_default_is_one_object_everywhere() -> None:
    from scripts import launcher_env, role_pane_entry, team_launcher

    default = launcher_env.DEFAULT_PANE_STATE_DIR
    for name in ("run_role_pane", "run_detached_role", "ensure_visible_role_session_for_viewer", "attach_role_to_slot"):
        param = inspect.signature(getattr(role_pane_entry, name)).parameters["pane_state_dir"]
        check(param.default is default, f"{name} defaults to the leaf's one pane-state directory")
    # Measured on the baseline: exactly these four took it as a default, and
    # no function that stays in the launcher does.
    with_path_default = sorted(
        fn.__name__ for fn in vars(team_launcher).values()
        if inspect.isfunction(fn) and "pane_state_dir" in inspect.signature(fn).parameters
        and inspect.signature(fn).parameters["pane_state_dir"].default not in (inspect.Parameter.empty, None)
    )
    check(with_path_default == sorted(["run_role_pane", "run_detached_role",
                                       "ensure_visible_role_session_for_viewer", "attach_role_to_slot"]),
          f"and no other launcher-reachable function carries a pane-state default of its own: {with_path_default}")
    check(team_launcher.DEFAULT_PANE_STATE_DIR is default, "the launcher's name is the leaf's object")


def test_rebinding_the_pane_state_default_still_reaches_session_paths() -> None:
    from scripts import session_paths, team_launcher

    marker = Path("/nonexistent/syrd-313/pane-state")
    saved = (team_launcher.DEFAULT_PANE_STATE_DIR, team_launcher._env_first)
    team_launcher.DEFAULT_PANE_STATE_DIR = marker
    team_launcher._env_first = lambda *names: ""
    try:
        resolved = session_paths.default_pane_state_dir_for_user("", project="p313")
    finally:
        team_launcher.DEFAULT_PANE_STATE_DIR, team_launcher._env_first = saved
    check(resolved == marker, f"a launcher rebinding still reaches session_paths: {resolved}")


def test_the_rebound_timings_and_roots_stay_the_launchers() -> None:
    from scripts import provider_resume, role_pane_entry, team_launcher

    for module in ("provider_resume.py", "role_pane_entry.py", "launcher_env.py"):
        tree = ast.parse((ROOT / "scripts" / module).read_text(encoding="utf-8"))
        bare = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in REBOUND})
        check(bare == [], f"{module} neither binds nor reads a rebound constant past the launcher: {bare}")
    check(all(hasattr(team_launcher, n) and not hasattr(provider_resume, n) and not hasattr(role_pane_entry, n)
              for n in REBOUND), "each rebound constant is the launcher's alone")
    with tempfile.TemporaryDirectory(prefix="syrd313.") as raw:
        root = Path(raw)
        (root / "conversations").mkdir()
        (root / "conversations" / "syrd-313.db").write_text("", encoding="utf-8")
        saved = team_launcher.AGY_CONVERSATION_ROOT
        team_launcher.AGY_CONVERSATION_ROOT = root
        try:
            found = provider_resume.agy_conversation_store_exists("syrd-313")
        finally:
            team_launcher.AGY_CONVERSATION_ROOT = saved
    check(found is True, "a rebound AGY_CONVERSATION_ROOT is where the resume check looks")


def test_the_patched_entry_points_are_called_by_the_launchers_name() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    phases = ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8"))
    for name in PATCHED_ENTRY_POINTS:
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        phase_calls = [n for n in ast.walk(phases)
                       if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(calls and len(calls) + len(phase_calls) == ENTRY_POINT_CALLS[name]
              and all(isinstance(n.func, ast.Name) for n in calls)
              and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id == "launcher" for n in phase_calls),
              f"the launcher calls {name} by its own patchable name, and launch_phases through it, "
              f"at its {ENTRY_POINT_CALLS[name]} baseline sites")
    moved = ast.parse((ROOT / "scripts" / "role_pane_entry.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in PATCHED_ENTRY_POINTS})
    check(bare == [], f"and pane entry never calls one past that patch: {bare}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"role_pane_entry_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
