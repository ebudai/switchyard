#!/usr/bin/env python3
"""SYRD-297: the provider auth-status module's boundary with the launcher it came out of.

Provider auth-status probing moved into `scripts/provider_auth_status.py`
unchanged. Its facilities are patched on `team_launcher` by the suites; the
status table is patched by rebinding it. This pins what makes that safe:

- The module does not import the launcher at its top: the launcher imports it.
- Every name is still `team_launcher.<name>`, the very same object, whichever
  module is imported first.
- **A rebound status table on the launcher reaches the moved code.**
  `_cli_auth_status` reads `launcher.FIRST_RUN_AUTH_STATUS_COMMANDS` when it
  runs, and the owner-context probe runs the patched command through the
  launcher's patched env and identity helpers.
- The other patched names (`_owner_home_for_auth`, `_cli_auth_status`,
  `_provider_account_setup_complete`) stay reachable, and patchable, on the
  launcher for the modules already moved out, which call them through it.

A recording runner only: no provider is run and nothing is signed in.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'FIRST_RUN_AUTH_STATUS_COMMANDS',
    '_owner_home_for_auth',
    'OWNER_CLI_PROBE_TIMEOUT_SECONDS',
    'PROBE_TIMED_OUT_STATUS',
    '_run_owner_cli_probe',
    '_cli_auth_probe_passed',
    '_owner_cli_is_installed',
    '_cli_auth_status',
    '_claude_account_setup_complete',
    '_provider_account_setup_complete',
)


def attempt(action):
    """Run one step and hand back what it returned or raised, so a check reports it."""
    try:
        return action(), None
    except BaseException as exc:  # noqa: BLE001 -- what escaped is the finding
        return None, exc


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_imports_without_the_launcher() -> None:
    result = python("import sys, scripts.provider_auth_status; print('scripts.team_launcher' in sys.modules)")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "False", f"and does not pull the launcher in: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.provider_auth_status", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.provider_auth_status")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.provider_auth_status as a; "
            f"print(all(getattr(t, n) is getattr(a, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_a_rebound_status_table_on_the_launcher_reaches_the_moved_probe() -> None:
    _, escaped_import = attempt(lambda: __import__("scripts.provider_auth_status"))
    check(escaped_import is None, f"scripts.provider_auth_status imports in this process: {escaped_import!r}")
    from scripts import provider_auth_status, team_launcher

    ran: list[tuple[list[str], dict]] = []

    def runner(args, **kwargs):
        ran.append((list(args), {k: kwargs[k] for k in ("cwd", "env", "timeout") if k in kwargs}))
        return subprocess.CompletedProcess(args, 0, "", "")

    names = ("FIRST_RUN_AUTH_STATUS_COMMANDS", "_owner_command_env_args", "_pane_identity_scrubbed_env")
    saved = {name: getattr(team_launcher, name) for name in names}
    team_launcher.FIRST_RUN_AUTH_STATUS_COMMANDS = {"fakecli": ["fakecli", "auth", "status"]}
    team_launcher._owner_command_env_args = lambda user, home, command: ["as-owner", user, str(home), *command]
    team_launcher._pane_identity_scrubbed_env = lambda: {"SCRUBBED": "1"}
    try:
        known, escaped_known = attempt(lambda: provider_auth_status._cli_auth_status(
            "fakecli", owner_user="owner-x", owner_home=Path("/homes/owner-x"), runner=runner))
        unlisted, escaped_unlisted = attempt(lambda: provider_auth_status._cli_auth_status(
            "claude", owner_user="owner-x", owner_home=Path("/homes/owner-x"), runner=runner))
    finally:
        for name, value in saved.items():
            setattr(team_launcher, name, value)
    check(known == "authenticated" and escaped_known is None,
          f"the probe of a CLI in the launcher's rebound table passed: {known!r} {escaped_known!r}")
    check(ran == [(["as-owner", "owner-x", "/homes/owner-x", "fakecli", "auth", "status"],
                   {"cwd": "/homes/owner-x", "env": {"SCRUBBED": "1"},
                    "timeout": provider_auth_status.OWNER_CLI_PROBE_TIMEOUT_SECONDS})],
          f"and ran exactly the rebound command, in the owner's context, via the launcher's helpers: {ran}")
    check(unlisted == "authenticated",
          f"a CLI missing from the rebound table has no probe to fail, so the moved code read the launcher's table: {unlisted!r} {escaped_unlisted!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"provider_auth_status_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
