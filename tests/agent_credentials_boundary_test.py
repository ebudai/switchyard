#!/usr/bin/env python3
"""SYRD-287: the credential modules' boundary with the launcher they came out of.

Agent credential sourcing moved into `scripts/agy_credential.py`, and role
seeding into `scripts/role_credentials.py`, unchanged. This pins what makes
that safe:

- Neither module imports the launcher at its top: the launcher imports them,
  so an import back would be a cycle. `agy_credential` stands on
  `role_credentials` and never the reverse.
- Every name callers reached as `team_launcher.<name>` is still there, and is
  the very same object, whichever module is imported first.
- The owner-traversal checks are defined in `role_credentials`, but the suites
  patch them on the launcher (`team_launcher._require_owner_home_traversable`).
  So both no-follow openers call them through the launcher, and a patch there
  reaches them. Driven here against real directories, stopping at that check
  before any privileged step.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

#: Every moved name the launcher still exports, by the module that now holds it.
EXPORTED = {
    "scripts.role_credentials": (
        "AGY_CREDENTIAL_DIR_NAME", "AGY_CREDENTIAL_TOKEN_NAME", "HERMES_OWNER_CREDENTIAL_DIR",
        "HERMES_PROVIDER_ENV_KEYS", "ROLE_CREDENTIAL_ARTIFACTS", "RoleCredentialArtifact",
        "_require_owner_home_traversable", "_require_owner_traversable", "_role_credential_target",
        "hermes_credential_target", "role_credential_artifacts", "role_credential_manifest",
        "seed_role_credential", "select_hermes_provider_env", "switchyard_seed_role_credentials_command",
    ),
    "scripts.agy_credential": (
        "AGY_CREDENTIAL_ABSENT", "AGY_CREDENTIAL_INSTALLED", "AGY_CREDENTIAL_UNUSABLE",
        "AGY_SOURCE_FROM_HOST", "AGY_SOURCE_FROM_OPT_OUT", "AGY_SOURCE_FROM_OVERRIDE", "AGY_SOURCE_UNSET",
        "DEFAULT_AGY_CREDENTIAL_SETTING_PATH", "_agy_credential_state", "_open_owner_credential_dir",
        "_resolve_agy_credential_source", "_seed_agy_credential_for_owner", "_validate_agy_credential_source",
        "read_host_agy_credential_source", "switchyard_agy_credential_command",
        "write_host_agy_credential_source",
    ),
}


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_each_module_imports_without_the_launcher() -> None:
    for module, also_absent in (("scripts.role_credentials", "scripts.agy_credential"),
                                ("scripts.agy_credential", None)):
        result = python(
            f"import sys, importlib; importlib.import_module({module!r}); "
            f"print('scripts.team_launcher' in sys.modules, {also_absent!r} in sys.modules)"
        )
        check(result.returncode == 0, f"{module} imports on its own: {result.stderr[-600:]}")
        check(result.stdout.split() == ["False", "False"],
              f"{module} pulls in neither the launcher nor a module above it: {result.stdout!r}")


def test_every_import_order_gives_one_set_of_objects() -> None:
    orders = (
        ("scripts.role_credentials", "scripts.agy_credential", "scripts.team_launcher"),
        ("scripts.agy_credential", "scripts.team_launcher", "scripts.role_credentials"),
        ("scripts.team_launcher", "scripts.role_credentials", "scripts.agy_credential"),
    )
    for order in orders:
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t; "
            f"table = {EXPORTED!r}; "
            "print(all(getattr(t, n) is getattr(importlib.import_module(m), n) "
            "for m, names in table.items() for n in names))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


class _Stopped(Exception):
    """Raised by the patched check, so nothing after it runs."""


def test_a_patched_traversal_check_on_the_launcher_reaches_both_openers() -> None:
    from scripts import agy_credential, role_credentials, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd287-seam.") as raw:
        homes = Path(raw) / "home"
        (homes / "otto-agent").mkdir(parents=True)
        (homes / "role-home").mkdir()
        asked: list[tuple[str, str]] = []

        def patched(fd: int, user: str, home: Path) -> None:
            check(os.path.isdir(f"/proc/self/fd/{fd}"), "it is handed the opened home, as a descriptor")
            asked.append((user, home.name))
            raise _Stopped

        saved = team_launcher._require_owner_home_traversable
        team_launcher._require_owner_home_traversable = patched
        ran: list = []
        try:
            for opener, args, expect in (
                (agy_credential._open_owner_credential_dir, ("otto-agent", homes), ("otto-agent", "otto-agent")),
                (role_credentials._open_role_credential_parent,
                 ("role-agent", homes / "role-home", ".hermes/auth.json"), ("role-agent", "role-home")),
            ):
                asked.clear()
                outcome: BaseException | None = None
                try:
                    opener(*args, runner=lambda args, **k: ran.append(args)
                           or subprocess.CompletedProcess(args, 0, "", ""))
                except BaseException as exc:  # noqa: BLE001 -- what escaped is the finding
                    outcome = exc
                check(isinstance(outcome, _Stopped) and asked == [expect],
                      f"{opener.__module__}.{opener.__name__} asked the launcher's patched check: "
                      f"asked {asked}, ended with {outcome!r}")
        finally:
            team_launcher._require_owner_home_traversable = saved
        check(ran == [], f"and nothing privileged ran before it: {ran}")
        check(sorted(p.name for p in homes.iterdir()) == ["otto-agent", "role-home"]
              and not any((homes / "otto-agent").iterdir()), "nothing was created past the check")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"agent_credentials_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
