#!/usr/bin/env python3
"""SYRD-305: role-account migration's boundary with the launcher it came out of.

The role-account migration -- the root script, its trusted published copy and
the operator's instruction -- moved into `scripts/role_account_migration.py`
unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher's `new`, `upgrade` and identities-cutover code and
  the suites reach as `team_launcher.<name>` is still there and is the very
  same object, whichever module is imported first.
- **Where root's copy lives is still the launcher's decision.** The trusted
  path is built from the launcher's privileged provision root, provision
  directory and migration name as they are when it is asked, and the trust
  walk stops at the launcher's redirected root -- so a suite that redirects
  the root on the launcher redirects this too.
- **The staging commands are the function's own import (SYRD-324).** The
  migration takes its role-tooling staging commands from its own
  function-level import of `project_provision`, as it did before SYRD-305 --
  never from the launcher's name, even when that is rebound -- while the
  shared install root is still read from the launcher when it runs.

Nothing here reads or writes a real account, ACL or privileged path.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'account_existence_guard',
    'director_control_access_commands_for',
    '_privileged_directory_is_closed',
    'publish_role_account_migration',
    'remove_untrusted_role_account_migration',
    'render_role_account_migration',
    'role_account_migration_instruction',
    'role_path_access_commands',
    'trusted_role_account_migration_path',
    'upgrade_role_accounts_in_config',
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
        "import sys, scripts.role_account_migration; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.role_account_migration'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.role_account_migration", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.role_account_migration")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.role_account_migration as r; "
            f"print(all(getattr(t, n) is getattr(r, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_roots_copy_is_placed_and_bounded_by_the_launcher_when_asked() -> None:
    from scripts import role_account_migration, team_launcher

    redirected = Path("/nonexistent/syrd-305/provision")
    names = ("switchyard_privileged_provision_root", "privileged_provision_dir", "role_account_migration_name")
    saved = {name: getattr(team_launcher, name) for name in names}
    team_launcher.switchyard_privileged_provision_root = lambda: redirected
    team_launcher.privileged_provision_dir = lambda project, root: root / f"dir-{project}"
    team_launcher.role_account_migration_name = lambda project: f"syrd-305-{project}.sh"
    try:
        path = role_account_migration.trusted_role_account_migration_path(SimpleNamespace(project="p305"))
        boundary = role_account_migration._privileged_artifact_boundary()
    finally:
        for name, value in saved.items():
            setattr(team_launcher, name, value)
    check(path == redirected / "dir-p305" / "syrd-305-p305.sh",
          f"the trusted copy is where the launcher's root, directory and name put it: {path}")
    check(boundary == redirected, f"and the trust walk stops at the launcher's redirected root: {boundary}")


def _tenant(tmp: Path):
    """A tenant rendered into a temporary directory, as the script-order suite builds one."""
    from scripts import team_launcher
    from scripts.ticket_board.project_provision import build_plan, write_artifacts

    provision, repository = tmp / "provision", tmp / "repository"
    provision.mkdir()
    repository.mkdir()
    plan = build_plan(project="p324", project_name="P324", owner_user="p324-agent", owner_home=tmp / "home",
                      source_repo=ROOT)
    write_artifacts(plan, provision, enable_owner_linger=False)
    config_path = team_launcher.write_new_project_launcher_artifacts(
        plan, provision, repository=repository, print_func=lambda _text: None)
    return team_launcher.load_project_config("p324", config_path), config_path


def test_the_staging_commands_are_the_functions_own_import() -> None:
    from scripts import role_account_migration, team_launcher
    from scripts.ticket_board import project_provision

    fn = [n for n in ast.parse((ROOT / "scripts" / "role_account_migration.py").read_text(encoding="utf-8")).body
          if isinstance(n, ast.FunctionDef) and n.name == "render_role_account_migration"][0]
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "role_tooling_staging_commands"]
    imported = [n for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
                and n.module == "scripts.ticket_board.project_provision"
                and any(a.name == "role_tooling_staging_commands" for a in n.names)]
    check(len(calls) == 1 and isinstance(calls[0].func, ast.Name) and len(imported) == 1,
          "the migration calls the name it imports from project_provision, not the launcher's")

    asked: list[tuple[str, str]] = []

    def provisioned(project: str, release: str, **kwargs: object) -> list[str]:
        asked.append((project, release))
        return ["# syrd324 staging from project_provision"]

    def trap(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("the migration reached team_launcher.role_tooling_staging_commands")

    with tempfile.TemporaryDirectory(prefix="syrd324.") as raw:
        config, config_path = _tenant(Path(raw))
        saved = (project_provision.role_tooling_staging_commands, team_launcher.role_tooling_staging_commands,
                 team_launcher.switchyard_shared_install_root)
        project_provision.role_tooling_staging_commands = provisioned
        team_launcher.role_tooling_staging_commands = trap
        team_launcher.switchyard_shared_install_root = lambda: Path("/nonexistent/syrd324/shared")
        try:
            script = role_account_migration.render_role_account_migration(config, config_path=config_path)
        finally:
            (project_provision.role_tooling_staging_commands, team_launcher.role_tooling_staging_commands,
             team_launcher.switchyard_shared_install_root) = saved
    check("# syrd324 staging from project_provision" in script.splitlines(),
          "the staging commands in the script are project_provision's, as the function imports them")
    check(asked == [("p324", "/nonexistent/syrd324/shared/current")],
          f"from the launcher's install root, read when the function runs: {asked}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"role_account_migration_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
