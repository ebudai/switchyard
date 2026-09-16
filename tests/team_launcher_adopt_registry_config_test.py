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

        # THE SECOND LIVE DEFECT (journal 0054). The registered configuration on
        # that host is mode 0660 -- `switchyard new` wrote some of them
        # group-writable -- and the verifier refuses a document anybody in its
        # group could rewrite. Correctly: that is the whole reason it reads the
        # mode. So the MODE is repaired before verification and the check runs
        # unchanged.
        config_path.chmod(0o660)
        with owner_patches(home):
            found, config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
            )
        check(found == config_path, f"a 0660 registered config is adoptable: {found} {problems}")
        check(config_path.stat().st_mode & 0o022 == 0,
              f"because the mode was repaired: {config_path.stat().st_mode & 0o777:04o}")
        check(config_path.stat().st_mode & 0o777 == 0o640,
              f"exactly the two write bits, nothing else: {config_path.stat().st_mode & 0o777:04o}")

        # The repair is the two bits and nothing else. A file the permitted
        # accounts do not own is not root's to repair -- it is left exactly as
        # it is and the reader refuses it, which says whose it is.
        foreign = home / "Projects" / CHECKOUT / ".switchyard" / "provision" / "foreign.json"
        foreign.write_text("{}\n", encoding="utf-8")
        foreign.chmod(0o666)
        with patch.object(launcher, "expected_privileged_uid", lambda: 4243):
            left = launcher.normalize_tenant_config_mode(foreign, permitted_uids=[4242, 4243])
        check(left == [], f"a foreign file is passed over quietly: {left}")
        check(foreign.stat().st_mode & 0o777 == 0o666,
              f"and untouched: {foreign.stat().st_mode & 0o777:04o}")

        # A generated configuration is written that way in the FIRST PLACE, so
        # the repair is for what is already on disk rather than a licence to
        # keep producing them. Checked on a freshly generated file, before
        # anything has had a chance to repair it.
        fresh_dir = home / "Projects" / CHECKOUT / ".switchyard" / "fresh"
        fresh_dir.mkdir(parents=True, exist_ok=True)
        # Under a umask that WOULD have produced a group-writable file, which is
        # how the live one came to be 0660 -- otherwise this only proves the
        # umask of whoever ran the suite.
        previous_umask = os.umask(0o002)
        try:
            with owner_patches(home):
                fresh = launcher.write_new_project_launcher_artifacts(
                    plan, fresh_dir, repository=checkout, print_func=quiet
                )
        finally:
            os.umask(previous_umask)
        check(fresh.stat().st_mode & 0o022 == 0,
              f"a generated config is not group-writable: {fresh.stat().st_mode & 0o777:04o}")

        # THE THIRD LIVE DEFECT (journal 0057). syrd was provisioned before
        # `inspector` existed, so its configuration and the board it is running
        # both name that role and root's old baseline does not. Refusing it is
        # refusing the tenant for having been provisioned earlier; deleting the
        # check would be trusting the tenant's own JSON about which roles exist.
        # So the board's declared workflow has to name the role too.
        as_provisioned = config_path.read_text(encoding="utf-8")
        legacy = json.loads(as_provisioned)
        highest = max(int(role.get("slot", 0)) for role in legacy["roles"])
        template = dict(legacy["roles"][0])
        template.update(
            role="inspector", target=f"{SLUG}-inspector:0.0", slot=highest + 1,
            label="Inspector",
            workdir=str(home / f"{SLUG}-worktrees" / "inspector"),
        )
        legacy["roles"].append(template)
        config_path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")

        def board_names(*roles):
            return lambda _config: ({"roles": [{"name": name} for name in roles]}, "")

        # The conflict check on its own, with corroboration explicitly off: an
        # added role is a conflict until something establishes it. (Asking for
        # the default here would be asking a different question -- since
        # SYRD-168 the default consults the board, which is the point of that
        # ticket.)
        with owner_patches(home):
            found, config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry,
                corroborate_roles=False,
            )
        check(found is None, f"unestablished, the added role is still refused: {found}")
        check(any("inspector" in line for line in problems), f"and named: {problems}")

        with owner_patches(home):
            found, config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry,
                board_reader=board_names("director", "app", "inspector"),
            )
        check(found == config_path,
              f"a role the running board declares too is established: {found} {problems}")

        # And a role only the tenant's file names is still refused, with the
        # board asked and answering.
        with owner_patches(home):
            found, config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry,
                board_reader=board_names("director", "app"),
            )
        check(found is None, f"an uncorroborated role is refused: {found}")
        check(any("the board's declared workflow does not name" in line for line in problems),
              f"and says what would have established it: {problems}")

        # A board that answers nothing corroborates nothing: an unreachable
        # board is not a licence to adopt whatever the file says.
        with owner_patches(home):
            found, _config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry,
                board_reader=lambda _config: (None, "board unreachable"),
            )
        check(found is None, f"no answer corroborates nothing: {found}")

        # Corroboration is reached ONLY when everything else already agrees, so
        # a configuration cannot nominate the authority that vouches for it: a
        # file that also disagrees about the socket is refused without the
        # board being asked at all.
        asked: list[str] = []

        def recording(_config):
            asked.append("asked")
            return ({"roles": [{"name": "inspector"}]}, "")

        elsewhere = json.loads(config_path.read_text(encoding="utf-8"))
        elsewhere["board_socket"] = "/tmp/somebody-elses.sock"
        config_path.write_text(json.dumps(elsewhere, indent=2), encoding="utf-8")
        with owner_patches(home):
            found, _config, problems = launcher.verified_tenant_config(
                plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry, board_reader=recording,
            )
        check(found is None, f"a configuration disagreeing elsewhere is refused: {found}")
        check(asked == [], f"and the board it nominated was never asked: {asked}")
        # Back to what provisioning wrote, so the cases after this one are about
        # what they say they are about.
        config_path.write_text(as_provisioned, encoding="utf-8")

        # SYRD-168: THE SAME CORROBORATION ON EVERY PRIVILEGED PATH. SYRD-167
        # wired the reader into adoption alone, so `resume-provision` called
        # this same verifier without one and refused `inspector` on a tenant
        # whose configuration, board workflow and live runtime assignments all
        # name it (journal 0068). The default is what fixes that: a caller no
        # longer has to remember.
        config_path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
        with owner_patches(home):
            with patch.object(launcher, "read_board_declared_workflow",
                              board_names("director", "app", "inspector")):
                found, _config, problems = launcher.verified_tenant_config(
                    plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
                )
        check(found == config_path,
              f"a caller that passes no reader still corroborates: {found} {problems}")

        # Fail-closed by default, not open: the same call with a board that does
        # not name the role, and with a board that cannot be reached at all.
        with owner_patches(home):
            with patch.object(launcher, "read_board_declared_workflow", board_names("director", "app")):
                found, _config, problems = launcher.verified_tenant_config(
                    plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
                )
        check(found is None, f"the default does not establish an unnamed role: {found}")
        with owner_patches(home):
            with patch.object(launcher, "read_board_declared_workflow",
                              lambda _config: (None, "board unreachable")):
                found, _config, problems = launcher.verified_tenant_config(
                    plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
                )
        check(found is None, f"an unreachable board corroborates nothing by default: {found}")

        # And the ordering survives the default: a configuration that also
        # disagrees about its socket never reaches the board it nominated.
        default_asked: list[str] = []

        def recording_default(_config):
            default_asked.append("asked")
            return ({"roles": [{"name": "inspector"}]}, "")

        moved_socket = dict(legacy, board_socket="/tmp/somebody-elses.sock")
        config_path.write_text(json.dumps(moved_socket, indent=2), encoding="utf-8")
        with owner_patches(home):
            with patch.object(launcher, "read_board_declared_workflow", recording_default):
                found, _config, problems = launcher.verified_tenant_config(
                    plan, SLUG, owner_uid=TENANT_UID, registry_dir=registry
                )
        check(found is None and default_asked == [],
              f"nominated authority is not asked, default or not: {found} {default_asked}")
        config_path.write_text(as_provisioned, encoding="utf-8")

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
