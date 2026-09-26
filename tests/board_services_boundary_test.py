#!/usr/bin/env python3
"""SYRD-302: board and listener service control's boundary with the launcher it came out of.

The service-control code moved into `scripts/board_services.py` unchanged.
This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher's lifecycle code (suspend, resume, cutover,
  upgrade, new) and the suites reach as `team_launcher.<name>` is still there
  and is the very same object, whichever module is imported first.
- **Patched names are read through the launcher.** The suites patch
  `capture_listener_state`, `board_system_unit_is_active`,
  `_ensure_board_service_peer_auth` and `_uid_for_user` on the launcher, and
  rebind `OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS` there. Moved code that
  reads any of them does so as `launcher.<name>`, never past the patch.
- A listener stop still builds its command with the launcher's owner-command
  builder and confirms the stop with the launcher's `capture_listener_state`.

Nothing here runs systemctl: the runner is a recorder and every command
builder it could reach is patched.
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

EXPORTED = (
    '_board_system_unit',
    '_board_system_unit_action',
    '_canary_system_unit',
    '_default_board_service_user',
    '_ensure_board_service_peer_auth',
    '_ensure_board_service_user',
    '_listener_user_unit',
    '_non_login_shell_path',
    'activate_board_authority',
    'authority_unit_installs',
    'board_service_user',
    'board_system_unit_is_active',
    'capture_listener_state',
    'managed_unit_names',
    'MANAGER_RESPONDING',
    'MANAGER_WEDGED',
    'OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS',
    'owner_user_manager_state',
    'repair_owner_user_manager',
    'restore_installed_units',
    'start_owner_listener',
    'stop_owner_listener',
)

#: Patched on the launcher by the suites (the timeout by rebinding).
PATCHED_ON_THE_LAUNCHER = (
    'OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS',
    '_ensure_board_service_peer_auth',
    '_uid_for_user',
    'board_system_unit_is_active',
    'capture_listener_state',
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.board_services; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.board_services'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.board_services", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.board_services")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.board_services as b; "
            f"print(all(getattr(t, n) is getattr(b, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_patched_names_are_only_read_through_the_launcher() -> None:
    tree = ast.parse((ROOT / "scripts" / "board_services.py").read_text(encoding="utf-8"))
    bare = sorted({
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in PATCHED_ON_THE_LAUNCHER
    })
    through = sorted({
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "launcher" and node.attr in PATCHED_ON_THE_LAUNCHER
    })
    check(bare == [], f"no patched name is read past the launcher's patch: {bare}")
    check(through == ["OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS", "_uid_for_user", "capture_listener_state"],
          f"and the ones the moved code reads, it reads through the launcher: {through}")


def test_a_listener_stop_uses_the_launchers_builder_and_confirmation() -> None:
    from scripts import board_services, team_launcher

    config = SimpleNamespace(project="syrd302", run_as_user="syrd-302-no-such-user")
    ran: list[list[str]] = []

    def recorder(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    saved = (team_launcher._owner_user_systemctl, team_launcher.capture_listener_state)
    team_launcher._owner_user_systemctl = lambda config, action, unit, config_path=None: ["syrd-302-argv", action, unit]
    team_launcher.capture_listener_state = lambda *args, **kwargs: "active"
    try:
        problems = board_services.stop_owner_listener(config, runner=recorder)
    finally:
        team_launcher._owner_user_systemctl, team_launcher.capture_listener_state = saved
    unit = "syrd302-ticket-board-notify-listener.service"
    check(ran == [["syrd-302-argv", "stop", unit]], f"the stop was built by the launcher's builder: {ran!r}")
    check(problems == [f"{unit} is still active after being stopped"],
          f"and confirmed with the launcher's capture_listener_state: {problems!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"board_services_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
