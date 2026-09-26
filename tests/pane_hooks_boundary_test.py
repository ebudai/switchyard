#!/usr/bin/env python3
"""SYRD-294: the pane-hooks module's boundary with the launcher it came out of.

Installing and refreshing a role's pane hooks, and reporting stale Codex hook
trust, moved into `scripts/pane_hooks.py` unchanged. This pins what makes that
safe:

- The module does not import the launcher at its top: the launcher imports it.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first.
- Launcher facilities the moved code uses are looked up on `team_launcher`
  when it runs, so a patch there reaches it: `home_dir_for_user`,
  `uid_for_user`, `runtime_dir_for_uid`, `current_user_name` and
  `_role_cli_name`. Nothing is installed; only argv is built.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

#: Every moved name the launcher still exports, fixed here so dropping one is noticed.
EXPORTED = (
    'install_generated_project_pane_hooks_args',
    'ensure_generated_project_pane_hooks',
    'tenant_hook_accounts',
    'refresh_role_pane_hooks',
    'CodexHookTrustMismatch',
    'stale_codex_hook_trust_for_roles',
    '_format_codex_hook_trust_report',
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
    result = python("import sys, scripts.pane_hooks; print('scripts.team_launcher' in sys.modules)")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "False", f"and does not pull the launcher in: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.pane_hooks", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.pane_hooks")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.pane_hooks as h; "
            f"print(all(getattr(t, n) is getattr(h, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_hook_installer_argv_is_built_from_the_launchers_patched_facilities() -> None:
    from scripts import pane_hooks, team_launcher

    config = SimpleNamespace(run_as_user="owner-x", pane_launcher=None, project="porter", session_dir=Path("/sessions"))
    names = ("home_dir_for_user", "uid_for_user", "runtime_dir_for_uid", "current_user_name")
    saved = {name: getattr(team_launcher, name) for name in names}
    team_launcher.home_dir_for_user = lambda user: Path("/homes") / user
    team_launcher.uid_for_user = lambda user: 4242
    team_launcher.runtime_dir_for_uid = lambda uid: Path(f"/run/user/{uid}")
    team_launcher.current_user_name = lambda: "someone-else"
    build = lambda: pane_hooks.install_generated_project_pane_hooks_args(
        config, script_path=Path("/opt/switchyard/scripts/team-launcher"), pane_state_dir=Path("/state"))
    try:
        args, escaped = attempt(build)
        team_launcher.current_user_name = lambda: "owner-x"
        as_owner, escaped_as_owner = attempt(build)
    finally:
        for name, value in saved.items():
            setattr(team_launcher, name, value)
    check(args == [
        "sudo", "-u", "owner-x", "-H", "env", "XDG_RUNTIME_DIR=/run/user/4242", "TICKET_BOARD_PROJECT=porter",
        "TICKET_BOARD_PANE_STATE_DIR=/state", "TICKET_BOARD_PANE_SESSION_DIR=/sessions",
        "/opt/switchyard/scripts/ticket-board-install-pane-hooks", "install", "--home", "/homes/owner-x",
        "--hook-source", "/opt/switchyard/scripts/ticket-board-pane-idle-hook",
        "--bin-path", "/homes/owner-x/.local/bin/ticket-board-pane-idle-hook",
    ], f"the installer argv came from the launcher's patched home, uid, runtime dir and user: {args} {escaped!r}")
    check(as_owner == args[4:],
          f"and when the launcher says the owner is running, no sudo: {as_owner} {escaped_as_owner!r}")


def test_the_trust_check_asks_the_launcher_which_roles_are_codex() -> None:
    from scripts import pane_hooks, team_launcher

    asked: list[str] = []
    roles = [SimpleNamespace(role="ops", cli=["whatever"]), SimpleNamespace(role="app", cli=["whatever"])]
    saved = team_launcher._role_cli_name
    team_launcher._role_cli_name = lambda role: asked.append(role.role) or "claude"
    try:
        with tempfile.TemporaryDirectory(prefix="syrd294-trust.") as raw:
            found, escaped = attempt(lambda: pane_hooks.stale_codex_hook_trust_for_roles(roles, owner_home=Path(raw)))
    finally:
        team_launcher._role_cli_name = saved
    check(found == [] and asked == ["ops", "app"],
          f"the launcher's patched CLI lookup decided no role is Codex: {found} {asked} {escaped!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"pane_hooks_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
