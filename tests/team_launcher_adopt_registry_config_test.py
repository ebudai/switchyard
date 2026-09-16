#!/usr/bin/env python3
"""SYRD-167: adoption guessed where a project's configuration was, and the host knew.

Live SYRD-146 journal 0052 ran the documented command for the tenant it was
written for:

    switchyard adopt-workflow syrd

`/etc/switchyard/projects/syrd.json` names
`/home/switchyard-agent/Projects/switchyard/.switchyard/provision/syrd.json`.
Discovery ignored that and tried two paths built from the display name and the
slug -- `Projects/Switchyard/...` and `Projects/syrd/...` -- neither of which
exists on that host, and refused before touching anything. A checkout whose
basename is not the slug is ordinary; the registry is the record of where the
configuration actually is.

So the registry is consulted, and that is ALL it buys: the path it names goes
through the same ownership, mode, symlink and agrees-with-the-plan checks as a
path typed by hand. These cases run as root inside a user namespace, because
the records are root-owned and a fixture cannot own a root-owned file otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher as launcher  # noqa: E402

SLUG = "syrd"
TENANT = "syrd-agent"
TENANT_UID = 62148
#: The whole point of the ticket: the checkout is neither the slug nor the
#: display name. Both guesses miss it; the registry does not.
CHECKOUT = "switchyard"
DISPLAY = "Switchyard"
SEAM = "SWITCHYARD_PRIVILEGED_PROVISION_ROOT"

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def quiet(*_args, **_kwargs) -> None:
    return None


def namespaces_available() -> bool:
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "true"], capture_output=True, text=True
    )
    return probe.returncode == 0


def owner_patches(home: Path):
    from contextlib import ExitStack
    from types import SimpleNamespace

    stack = ExitStack()
    stack.enter_context(
        patch.object(launcher, "uid_for_user", lambda name: TENANT_UID if name == TENANT else None)
    )
    stack.enter_context(
        patch.object(launcher, "home_dir_for_user", lambda name: home if name == TENANT else None)
    )
    real = launcher.pwd.getpwuid
    stack.enter_context(
        patch.object(
            launcher.pwd, "getpwuid",
            side_effect=lambda value: SimpleNamespace(pw_gid=value) if value == TENANT_UID else real(value),
        )
    )
    return stack


def fixture(root: Path):
    """A tenant whose checkout basename matches neither its slug nor its name."""
    from scripts.ticket_board.project_provision import build_plan

    release = root / "release"
    (release / "scripts").mkdir(parents=True)
    home = root / "home" / TENANT
    checkout = home / "Projects" / CHECKOUT
    provision = checkout / ".switchyard" / "provision"
    provision.mkdir(parents=True)
    plan = build_plan(
        project=SLUG, project_name=DISPLAY, owner_user=TENANT, owner_home=home,
        source_repo=release, service_user="boardsvc",
        project_repository=checkout,
    )
    with owner_patches(home):
        config_path = launcher.write_new_project_launcher_artifacts(
            plan, provision, repository=checkout, print_func=quiet
        )
    return plan, home, checkout, config_path


def registry_entry(registry: Path, config_path: Path | str) -> Path:
    entry = registry / f"{SLUG}.json"
    entry.write_text(
        json.dumps(
            {
                "schema": launcher.SWITCHYARD_REGISTRY_SCHEMA,
                "slug": SLUG,
                "name": DISPLAY,
                "config_path": str(config_path),
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    entry.chmod(0o644)
    return entry


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd167-adopt.") as tmp:
        root = Path(tmp)
        os.environ[SEAM] = str(root / "provision")
        (root / "provision").mkdir(parents=True)
        registry = root / "registry"
        registry.mkdir()
        plan, home, checkout, config_path = fixture(root)

        # 1 and 2. THE LIVE CASE. The registry names the real configuration, in
        # a checkout named neither `syrd` nor `Switchyard`, and it verifies.
        registry_entry(registry, config_path)
        with owner_patches(home):
            found, config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
            )
        check(found == config_path, f"the registered path is the one verified: {found} {problems}")
        check(config is not None and config.project == SLUG, f"and it loads: {problems}")

        # And the guesses it replaced would both have missed.
        for guess in (home / "Projects" / DISPLAY, home / "Projects" / SLUG):
            check(not (guess / ".switchyard" / "provision" / f"{SLUG}.json").exists(),
                  f"the guessed path {guess} does not exist on a host like this")

        # 3. A MOVED CHECKOUT. The registry still names where it used to be, so
        # adoption refuses and says which path it could not read -- and an
        # explicit path is still the way through, which is what the rollout
        # workaround relied on.
        moved = home / "Projects" / "moved-elsewhere"
        (moved / ".switchyard" / "provision").mkdir(parents=True)
        relocated = Path(
            str(config_path).replace(str(checkout), str(moved))
        )
        config_path.replace(relocated)
        with owner_patches(home):
            found, config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
            )
        check(found is None and config is None, f"a stale registry path is not resolved: {found}")
        check(any(str(config_path) in line for line in problems),
              f"and the path it could not read is named: {problems}")
        with owner_patches(home):
            found, config, problems = launcher.verified_tenant_config(
                plan, SLUG, explicit=relocated, owner_uid=TENANT_UID, registry_dir=registry
            )
        check(found == relocated, f"an explicit path still works: {found} {problems}")
        relocated.replace(config_path)

        # 4. A REGISTRY THAT POINTS SOMEWHERE ELSE. A registry a tenant could
        # rewrite would otherwise be a tenant choosing which document root
        # adopts. The pointer buys a look, not a pass: the document is loaded
        # and then refused for disagreeing with what root provisioned, and
        # discovery does NOT fall back to a guess afterwards -- falling back is
        # how a refusal turns into "try the next one until something passes".
        hostile_dir = home / "Projects" / "hostile" / ".switchyard" / "provision"
        hostile_dir.mkdir(parents=True)
        hostile = hostile_dir / f"{SLUG}.json"
        altered = json.loads(config_path.read_text(encoding="utf-8"))
        # A field that still loads as a configuration, so the refusal comes from
        # disagreeing with what root provisioned rather than from being
        # unreadable -- the harder of the two to get right.
        altered["board_url"] = "http://127.0.0.1:65530"
        hostile.write_text(json.dumps(altered, indent=2), encoding="utf-8")
        registry_entry(registry, hostile)
        with owner_patches(home):
            found, config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
            )
        check(found is None and config is None, f"a mismatched registry path is refused: {found}")
        check(any(str(hostile) in line for line in problems),
              f"naming the document it refused: {problems}")
        check(any("is not the configuration root provisioned" in line for line in problems),
              f"and refused for the disagreement itself: {problems}")
        check(not any(str(config_path) in line for line in problems),
              f"without quietly trying the real one instead: {problems}")

        # A REGISTRY ENTRY THAT IS NOT ROOT'S IS NOT A RECORD. Asked of the
        # check itself: inside this namespace root is the only uid there is, so
        # the uid that counts as root's is named here instead and the entry
        # belongs to somebody else. A registry the tenant could write would be
        # the tenant choosing which document root adopts.
        registry_entry(registry, config_path)
        with patch.object(launcher, "expected_privileged_uid", lambda: 4243):
            check(launcher.registered_tenant_config_path(SLUG, registry_dir=registry) is None,
                  "a registry entry root does not own names nothing")
            with owner_patches(home):
                found, config, problems = launcher.verified_tenant_config(
                    plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
                )
            check(found is None,
                  f"and discovery falls back to the guesses rather than following it: {found}")

        # A registry naming a relative path is not a pointer at all.
        registry_entry(registry, "relative/../provision.json")
        check(launcher.registered_tenant_config_path(SLUG, registry_dir=registry) is None,
              "a relative registry path resolves against nothing and is ignored")

        # 5. BEFORE REGISTRATION, NOTHING CHANGES. With no entry at all the two
        # conventional guesses are exactly what is tried, in the order they
        # always were.
        (registry / f"{SLUG}.json").unlink()
        check(launcher.registered_tenant_config_path(SLUG, registry_dir=registry) is None,
              "no entry, no registered path")
        candidates = launcher._tenant_config_candidates(
            plan, SLUG, explicit=None, recorded=None, registered=None
        )
        check(candidates == [
            home / "Projects" / DISPLAY / ".switchyard" / "provision" / f"{SLUG}.json",
            home / "Projects" / SLUG / ".switchyard" / "provision" / f"{SLUG}.json",
        ], f"the pre-registration guesses are unchanged: {candidates}")

        # And the order of precedence, stated once where it is decided.
        explicit = Path("/tmp/explicit.json")
        recorded = Path("/tmp/recorded.json")
        registered = Path("/tmp/registered.json")
        check(launcher._tenant_config_candidates(
            plan, SLUG, explicit=explicit, recorded=recorded, registered=registered) == [explicit],
            "an explicit path is the whole answer")
        check(launcher._tenant_config_candidates(
            plan, SLUG, explicit=None, recorded=recorded, registered=registered) == [recorded],
            "then a path root has already verified")
        check(launcher._tenant_config_candidates(
            plan, SLUG, explicit=None, recorded=None, registered=registered) == [registered],
            "then the registry, before any guess")

    print(f"team_launcher_adopt_registry_config_test: {CHECKS} checks ok")
    return 0


def main() -> int:
    assert namespaces_available(), (
        "these cases need a user namespace: the records they read are root-owned, "
        "and a fixture cannot own one otherwise. This suite fails rather than skipping."
    )
    done = subprocess.run(
        ["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
        text=True,
    )
    return done.returncode


if __name__ == "__main__":
    if "--privileged-child" in sys.argv:
        raise SystemExit(privileged_cases())
    raise SystemExit(main())
