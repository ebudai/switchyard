#!/usr/bin/env python3
"""SYRD-300: the first-run authentication phase's boundary with the launcher it came out of.

The auth phase and its report moved into `scripts/first_run_auth.py`
unchanged. This pins what makes that safe:

- **No cycle.** The phase imports the session runner and the runtime catalog
  at its top, never the launcher, and the launcher imports it at its top.
- Every name the launcher and the suites reach as `team_launcher.<name>` is
  still there and is the very same object, whichever module is imported first.
  The timeout and purposes it defaults to are the session runner's own.
- **One runner sentinel.** `switchyard new` and the phase both default to
  `NO_RUNNER_INJECTED`, and only that object means "watch the windows"
  (SYRD-221). A second copy would silently turn the live path unwatched.
- **The launcher seams still reach the moved code.** The suites patch the
  step runners on the launcher, so the phase calls them through it; the
  report reads the install clause and the login table from the launcher when
  it runs, so a patch there is what it prints.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'NO_RUNNER_INJECTED',
    'FirstRunAuthReport',
    'foreground_runner_for',
    '_NoRunnerInjected',
    'OwnerShellIssue',
    'report_first_run_auth_warnings',
    '_run_owner_cli_until',
    'run_first_run_auth_phase',
    'stop_before_launch_for_unauthenticated_providers',
)

FROM_THE_SESSION = ('FOREGROUND_COMPLETION_TIMEOUT_SECONDS', 'SETUP_PURPOSE_FOLDER_TRUST', 'SETUP_PURPOSE_SIGN_IN')

#: Patched on the launcher by the suites, so the phase must call them there.
PATCHED_STEP_RUNNERS = ('_run_owner_cli_until', '_run_provider_first_run', 'run_provider_first_run_session')

LOADED_AT_IMPORT = [
    'scripts.provider_screen',
    'scripts.provider_session',
    'scripts.ticket_board',
    'scripts.ticket_board.prompt_schema',
    'scripts.ticket_board.runtime_catalog',
]


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_phase_never_loads_the_launcher_at_import() -> None:
    result = python(
        "import sys, scripts.first_run_auth; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.first_run_auth'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(eval(result.stdout.strip()) == LOADED_AT_IMPORT,
          f"and loads the session runner and the catalog, never the launcher: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.first_run_auth", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.first_run_auth")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.first_run_auth as a, scripts.provider_session as s; "
            f"print(all(getattr(t, n) is getattr(a, n) for n in {EXPORTED!r}), "
            f"all(getattr(a, n) is getattr(s, n) for n in {FROM_THE_SESSION!r}))"
        )
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: every moved name is the launcher's too, and the defaults are "
              f"the session runner's: {result.stdout}{result.stderr[-600:]}")


def test_one_sentinel_means_watch_the_windows() -> None:
    from scripts import first_run_auth, team_launcher

    sentinel = first_run_auth.NO_RUNNER_INJECTED
    new_default = inspect.signature(team_launcher.switchyard_new_command).parameters["runner"].default
    phase_default = inspect.signature(first_run_auth.run_first_run_auth_phase).parameters["foreground_runner"].default
    check(new_default is sentinel, "`switchyard new` defaults to the phase's own sentinel")
    check(phase_default is sentinel, "and so does the phase")
    check(first_run_auth.foreground_runner_for(new_default) is None,
          "which is read as the watched path, not as a runner")


def test_the_phase_calls_the_patched_step_runners_through_the_launcher() -> None:
    tree = ast.parse((ROOT / "scripts" / "first_run_auth.py").read_text(encoding="utf-8"))
    bare = sorted({
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in PATCHED_STEP_RUNNERS
    })
    through = sorted({
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "launcher"
        and node.func.attr in PATCHED_STEP_RUNNERS
    })
    check(bare == [], f"no step runner is called past the launcher's patches: {bare}")
    check(through == sorted(PATCHED_STEP_RUNNERS), f"every one is called through it: {through}")


def test_the_report_prints_what_the_launcher_holds_when_it_runs() -> None:
    from scripts import first_run_auth, team_launcher

    printed: list[str] = []
    saved = (team_launcher._missing_cli_install_clause, team_launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS)
    team_launcher._missing_cli_install_clause = lambda cli: f"syrd-300-install-clause-for-{cli}"
    team_launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS = {"claude": ["syrd-300-login-command"]}
    try:
        first_run_auth.report_first_run_auth_warnings(
            first_run_auth.FirstRunAuthReport(
                unauthenticated_roles={},
                untrusted_roles=[],
                missing_cli_roles={"codex": ["app"]},
                incomplete_provider_setup=[("claude", ["main"])],
                owner_user="syrd-300-no-such-user",
            ),
            print_func=printed.append,
        )
    finally:
        team_launcher._missing_cli_install_clause, team_launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS = saved
    text = "\n".join(printed)
    check("syrd-300-install-clause-for-codex" in text, f"the missing CLI names the launcher's clause: {text}")
    check("run `syrd-300-login-command` as syrd-300-no-such-user" in text,
          f"and the resume line the launcher's login command: {text}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"first_run_auth_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
