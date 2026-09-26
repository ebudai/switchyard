#!/usr/bin/env python3
"""SYRD-296: the first-run setup module's boundary with the launcher it came out of.

The first-run setup manifest and workdir-trust probes moved into
`scripts/first_run_setup.py` unchanged. The auth phase stayed in the launcher.
This pins what makes that safe:

- The module does not import the launcher at its top: the launcher imports it.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first.
- **The trust-probe seam stays on the launcher.** The manifest calls
  `launcher._workdir_is_trusted`, so a patch there observes and decides every
  probe. `team_launcher_first_run_models_test` relies on that to assert that
  detached roles are never probed.
- The auth facilities the manifest reads (`_cli_auth_status`,
  `_provider_account_setup_complete`, `_role_cli_name`,
  `stale_codex_hook_trust_for_roles`) are looked up on the launcher when it
  runs.

Canned answers only: no provider, login or trust record is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

#: Every moved name the launcher still exports, fixed here so dropping one is noticed.
EXPORTED = (
    'FirstRunAuthLoginStep',
    'FirstRunProviderSetupStep',
    'FirstRunFolderTrustStep',
    'FirstRunSetupManifest',
    'FIRST_RUN_TRUST_CLIS',
    '_role_names',
    '_git_common_dir_for',
    '_workdir_is_trusted',
    'build_first_run_setup_manifest',
    '_format_first_run_setup_manifest',
    'print_first_run_setup_manifest',
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
    result = python("import sys, scripts.first_run_setup; print('scripts.team_launcher' in sys.modules)")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "False", f"and does not pull the launcher in: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.first_run_setup", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.first_run_setup")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.first_run_setup as f; "
            f"print(all(getattr(t, n) is getattr(f, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_manifest_asks_the_launcher_whether_each_workdir_is_trusted() -> None:
    _, escaped_import = attempt(lambda: __import__("scripts.first_run_setup"))
    check(escaped_import is None, f"scripts.first_run_setup imports in this process: {escaped_import!r}")
    from scripts import first_run_setup, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd296-manifest.") as raw:
        tmp = Path(raw)
        path = tmp / "porter.json"
        path.write_text(json.dumps({"project": "porter", "board_url": "http://127.0.0.1:1/", "roles": [
            {"role": "main", "cli": ["claude"], "slot": 0, "workdir": str(tmp / "main")},
            {"role": "app", "cli": ["claude"], "slot": 1, "workdir": str(tmp / "app")},
        ]}), encoding="utf-8")
        config = team_launcher.load_project_config("porter", path)
        probes: list[str] = []
        names = ("_workdir_is_trusted", "_cli_auth_status", "_provider_account_setup_complete",
                 "stale_codex_hook_trust_for_roles")
        saved = {name: getattr(team_launcher, name) for name in names}
        team_launcher._cli_auth_status = lambda cli, **k: "authenticated"
        team_launcher._provider_account_setup_complete = lambda cli, **k: True
        team_launcher.stale_codex_hook_trust_for_roles = lambda roles, **k: []
        team_launcher._workdir_is_trusted = lambda cli, *, owner_home, workdir: probes.append(workdir.name) or workdir.name == "app"
        try:
            manifest, escaped = attempt(lambda: first_run_setup.build_first_run_setup_manifest(
                config, owner_user="syrd296-no-such-owner", owner_home=tmp / "home", runner=None))
        finally:
            for name, value in saved.items():
                setattr(team_launcher, name, value)
    check(sorted(probes) == ["app", "main"],
          f"every role's workdir was probed through the launcher's patched check: {probes} {escaped!r}")
    steps = [(step.role, step.workdir.name) for step in getattr(manifest, "folder_trust_steps", [])]
    check(steps == [("main", "main")],
          f"and only the workdir the patched check called untrusted needs a trust step: {steps}")
    check(getattr(manifest, "login_steps", None) == [] and getattr(manifest, "provider_setup_steps", None) == [],
          f"the patched auth facilities on the launcher decided no login or setup is needed: {manifest}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"first_run_setup_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
