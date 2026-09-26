#!/usr/bin/env python3
"""SYRD-311: session and pane-state paths' boundary with the launcher they came out of.

The session/pane-state directory resolvers and the initial pane idle state
moved into `scripts/session_paths.py` unchanged. The process-wide defaults
did not. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher and the modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **The defaults stay the launcher's.** `DEFAULT_SESSION_DIR`,
  `DEFAULT_PANE_STATE_DIR` and `LIVE_PGU_STATE_DIR_NAME` are defined only in
  the launcher, and the suites rebind them there: a rebinding must reach the
  moved resolvers, exactly as it reached them when they read the launcher's
  own globals.
- **Patched helpers are still reached.** A role's session directory asks the
  launcher's `account_session_dir`, and an idle-state seed is written by the
  launcher's writer under the launcher's file name.

Everything here uses temporary paths and patched lookups; nothing reads a
real home or writes real state.
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
    'account_session_dir',
    'clear_pane_idle_state_for_role',
    'default_pane_state_dir_for_user',
    'default_session_dir_for_user',
    'role_pane_state_dir',
    'role_session_dir',
    'seed_initial_pane_idle_state',
    'session_dir_uses_user_runtime',
    'shared_pane_state_dir',
)
STAY_IN_THE_LAUNCHER = ("DEFAULT_SESSION_DIR", "DEFAULT_PANE_STATE_DIR", "LIVE_PGU_STATE_DIR_NAME")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    """Rebind launcher attributes for one block, as the suites do."""

    def __init__(self, **values: object) -> None:
        from scripts import team_launcher

        self.launcher = team_launcher
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.launcher, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.launcher, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.launcher, name, value)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.session_paths; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.session_paths'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.session_paths", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.session_paths")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.session_paths as p; "
            f"print(all(getattr(t, n) is getattr(p, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_defaults_are_defined_only_in_the_launcher_and_read_there() -> None:
    from scripts import session_paths, team_launcher

    tree = ast.parse((ROOT / "scripts" / "session_paths.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in STAY_IN_THE_LAUNCHER})
    check(bare == [], f"the moved code neither binds nor reads a default past the launcher: {bare}")
    check(all(hasattr(team_launcher, name) and not hasattr(session_paths, name) for name in STAY_IN_THE_LAUNCHER),
          "and each default is the launcher's alone")


def test_rebinding_the_session_default_reaches_the_moved_resolvers() -> None:
    from scripts import session_paths

    marker = Path("/nonexistent/syrd-311/sessions")
    with patched(DEFAULT_SESSION_DIR=marker, home_dir_for_user=lambda user: None, _env_first=lambda *names: ""):
        owner = session_paths.default_session_dir_for_user("syrd-311-no-such-user")
        account = session_paths.account_session_dir("syrd-311-no-such-account", project="p311")
    check(owner == marker, f"the owner's default session dir is the rebound default: {owner}")
    check(account == marker, f"and so is an account's, when it has no home: {account}")


def test_rebinding_the_pane_state_default_reaches_the_moved_resolver() -> None:
    from scripts import session_paths

    marker = Path("/nonexistent/syrd-311/pane-state")
    with patched(DEFAULT_PANE_STATE_DIR=marker, _env_first=lambda *names: ""):
        resolved = session_paths.default_pane_state_dir_for_user("", project="p311")
    check(resolved == marker, f"an ownerless pane-state dir is the rebound default: {resolved}")


def test_a_roles_session_dir_asks_the_launchers_account_helper() -> None:
    from scripts import session_paths

    marker = Path("/nonexistent/syrd-311/account-sessions")
    config = SimpleNamespace(role_state_isolation=False, run_as_user="syrd-311-owner", project="p311",
                             session_dir=Path("/nonexistent/syrd-311/owner-sessions"))
    with patched(role_run_as_user=lambda config, role: "syrd-311-role-account",
                 account_session_dir=lambda account, project: marker):
        resolved = session_paths.role_session_dir(config, SimpleNamespace(role="main"))
    check(resolved == marker, f"a role on its own account resolves through the launcher's patched helper: {resolved}")


def test_an_idle_state_seed_is_written_by_the_launchers_writer() -> None:
    from scripts import session_paths

    written: list[tuple[Path, dict[str, object]]] = []
    state_dir = Path("/nonexistent/syrd-311/pane-state")
    with patched(pane_state_file_name=lambda target: f"syrd-311-{target}.json",
                 _write_json_atomic=lambda path, payload: written.append((path, dict(payload)))):
        path = session_paths.seed_initial_pane_idle_state(
            SimpleNamespace(target="p311-main"), pane_state_dir=state_dir, source="syrd-311", now=1.0)
    check(path == state_dir / "syrd-311-p311-main.json"
          and written == [(path, {"target": "p311-main", "state": "idle", "updated_at": 1.0, "source": "syrd-311"})],
          f"under the launcher's file name, through its writer: {written!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"session_paths_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
