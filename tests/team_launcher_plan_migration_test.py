#!/usr/bin/env python3
"""SYRD-52: a plan written by an older release must be migrated, not rejected.

Every release that adds a field to the generated plan used to make every
already provisioned tenant unupgradable: `refresh_generated_project_runtime_artifacts`
parses the tenant's document straight into the current strict shape before it
can reach the root-owned baseline or regenerate anything, so the live board
stopped at `plan.json is missing provision field 'role_control_sudoers_name'`.

The migration is derived, never guessed: a field the plan declares a default
for takes that default -- which for the authority fields is the "not configured
yet" state -- and a field without one is a generated name taken from a
reference plan built by the same `build_plan` provisioning uses. The root
branch runs in a user namespace against a genuinely distinct tenant uid.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher
from scripts.ticket_board.project_provision import (
    PLAN_BASELINE_FIELDS,
    build_plan,
    plan_field_defaults,
    plan_field_names,
    write_artifacts,
)

# The field set of a plan written before the current release: exactly the keys
# the live registered tenant's plan.json carries. Fixture data, so the cases
# below are the shape that actually failed rather than one invented to pass.
LEGACY_PLAN_FIELDS = (
    "admin_database_url",
    "asset_dir",
    "assignee_roles",
    "audit_roles",
    "board_current",
    "board_database_url",
    "board_log",
    "board_root",
    "board_service_traversal",
    "board_unit",
    "caller_roles",
    "canary_unit",
    "commit_git_dir",
    "database",
    "draft_roles",
    "frame_dir",
    "implementer_roles",
    "listener_database_url",
    "listener_log",
    "listener_role",
    "listener_unit",
    "operation_allowed_roles",
    "owner_home",
    "owner_user",
    "polkit_name",
    "port",
    "project",
    "project_name",
    "runtime_directory",
    "service_role",
    "service_user",
    "socket_path",
    "source_repo",
    "ticket_prefix",
    "tmpfiles_name",
    "workflow",
    "workflow_seed",
)


def fields_added_since_the_legacy_release() -> tuple[str, ...]:
    return tuple(name for name in plan_field_names() if name not in LEGACY_PLAN_FIELDS)


def write_legacy_plan(plan_path: Path) -> tuple[str, ...]:
    """Roll a current plan.json back to the field set an older release wrote."""
    stored = json.loads(plan_path.read_text(encoding="utf-8"))
    dropped = tuple(sorted(name for name in stored if name not in LEGACY_PLAN_FIELDS))
    for name in dropped:
        del stored[name]
    plan_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dropped


def test_the_shape_that_failed_is_the_shape_under_test() -> None:
    """The live tenant stopped on role_control_sudoers_name; SYRD-50 added two more."""
    # The registered tenant's own plan satisfies the declared baseline, so it is
    # an older plan to be migrated rather than a document to be refused.
    assert PLAN_BASELINE_FIELDS <= set(LEGACY_PLAN_FIELDS), sorted(
        PLAN_BASELINE_FIELDS - set(LEGACY_PLAN_FIELDS)
    )
    added = fields_added_since_the_legacy_release()
    assert "role_control_sudoers_name" in added, added
    # SYRD-50's passwordless tenant control.
    assert "tenant_control_sudoers_name" in added, added
    assert "control_user" in added, added
    # SYRD-39's per-role accounts, absent from any plan written before it.
    for name in ("role_accounts", "roles_group", "role_worktrees"):
        assert name in added, added


def test_the_migration_is_derived_rather_than_hardcoded() -> None:
    """Two unrelated tenants, and every filled field tracks that tenant's own data."""
    tenants = (
        ("porter", "porter-agent", "/srv/porter-home", 20101, "Porter Board"),
        ("mefp", "mefp-owner", "/var/lib/mefp", 41999, "MEFP"),
    )
    defaults = plan_field_defaults()
    with tempfile.TemporaryDirectory(prefix="plan-migration-derivation.") as tmp:
        for project, owner, home, port, name in tenants:
            reference = build_plan(
                project=project,
                project_name=name,
                owner_user=owner,
                owner_home=Path(home),
                port=port,
                source_repo=ROOT,
            )
            plan_path = Path(tmp) / f"{project}.json"
            write_artifacts(reference, Path(tmp), enable_owner_linger=False)
            shutil.move(str(Path(tmp) / "plan.json"), plan_path)
            dropped = write_legacy_plan(plan_path)
            assert dropped == tuple(sorted(fields_added_since_the_legacy_release())), dropped

            migrated: list[str] = []
            plan = launcher._project_board_provision_from_json(plan_path, migrated=migrated)
            assert sorted(migrated) == sorted(dropped), (migrated, dropped)
            for field in dropped:
                if field in defaults:
                    # A field the plan declares a default for is a thing this
                    # tenant has not configured. Filling it from the reference
                    # would invent a control bridge or a role-account table for
                    # accounts that do not exist.
                    assert getattr(plan, field) == defaults[field], field
                else:
                    assert getattr(plan, field) == getattr(reference, field), field
            # Nothing the document already recorded is replaced.
            recorded = json.loads(plan_path.read_text(encoding="utf-8"))
            for field in recorded:
                if field in ("workflow", "operation_allowed_roles"):
                    continue
                value = getattr(plan, field)
                stored = recorded[field]
                if isinstance(value, tuple):
                    value = list(value)
                assert value == stored, (project, field, value, stored)
            # And the derived names are this tenant's, not the other's.
            assert plan.role_control_sudoers_name == reference.role_control_sudoers_name
            assert project in plan.role_control_sudoers_name
            assert project in plan.tenant_control_sudoers_name
            assert plan.owner_home == home and plan.port == port


def test_a_document_that_is_not_a_plan_is_still_refused() -> None:
    """Migration completes an older plan; it must not invent a tenant.

    A stub naming only an identity records no board root, database or release
    path, and filling those from a reference would fabricate a tenant that was
    never provisioned -- and hand it to the release and rollback paths as if it
    had been.
    """
    with tempfile.TemporaryDirectory(prefix="plan-migration-refusal.") as tmp:
        plan_path = Path(tmp) / "plan.json"
        plan_path.write_text(json.dumps({"project": "porter"}), encoding="utf-8")
        try:
            launcher._project_board_provision_from_json(plan_path)
        except SystemExit as exc:
            assert "owner_home" in str(exc), exc
        else:
            raise AssertionError("a plan with no identity was accepted")

        stub = {
            "project": "porter",
            "owner_user": "porter-agent",
            "owner_home": "/srv/porter-home",
        }
        plan_path.write_text(json.dumps(stub), encoding="utf-8")
        try:
            launcher._project_board_provision_from_json(plan_path)
        except SystemExit as exc:
            assert "is missing provision field" in str(exc), exc
        else:
            raise AssertionError("a stub was completed into a tenant")

        # Dropping one baseline field from a real plan is refused the same way:
        # the migration fills what a release added, never what a plan lost.
        reference = build_plan(
            project="porter",
            owner_user="porter-agent",
            owner_home=Path("/srv/porter-home"),
            source_repo=ROOT,
        )
        write_artifacts(reference, Path(tmp), enable_owner_linger=False)
        write_legacy_plan(plan_path.with_name("plan.json"))
        complete = json.loads((Path(tmp) / "plan.json").read_text(encoding="utf-8"))
        for field in sorted(PLAN_BASELINE_FIELDS):
            damaged = {name: value for name, value in complete.items() if name != field}
            plan_path.write_text(json.dumps(damaged), encoding="utf-8")
            try:
                launcher._project_board_provision_from_json(plan_path)
            except SystemExit as exc:
                assert field in str(exc) or "owner_home" in str(exc), (field, exc)
                continue
            raise AssertionError(f"a plan missing {field} was completed")


def _fixture_tenant(root: Path, owner: str, *, project: str = "porter") -> tuple[Path, Path, object]:
    """A provisioned tenant whose plan.json is then rolled back to the old shape."""
    provision = root / "provision"
    provision.mkdir()
    repository = root / "repository"
    repository.mkdir()
    plan = build_plan(
        project=project,
        project_name=project.title(),
        owner_user=owner,
        owner_home=Path(pwd.getpwnam(owner).pw_dir),
        source_repo=ROOT,
    )
    write_artifacts(plan, provision, enable_owner_linger=False)
    config_path = launcher.write_new_project_launcher_artifacts(
        plan, provision, repository=repository, print_func=lambda _text: None
    )
    return provision, config_path, plan


def unprivileged_migration() -> None:
    """The branch a tenant runs: no root, so only the tenant's own copies move."""
    project = "porter"
    owner = pwd.getpwuid(os.geteuid()).pw_name
    with tempfile.TemporaryDirectory(prefix="plan-migration-tenant.") as tmp:
        root = Path(tmp)
        provision, config_path, plan = _fixture_tenant(root, owner, project=project)
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(root / "etc-switchyard")
        plan_path = provision / "plan.json"
        dropped = write_legacy_plan(plan_path)
        config = launcher.load_project_config(project, config_path)

        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "", "")

        before = plan_path.read_text(encoding="utf-8")
        outcome = launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=config_path, dry_run=True, runner=runner, print_func=lambda _t: None
        )
        # It reports the migration, and it reports it without writing.
        assert not outcome.changed, outcome.message
        assert "migrated" in outcome.message, outcome.message
        for field in dropped:
            assert field in outcome.message, (field, outcome.message)
        assert plan_path.read_text(encoding="utf-8") == before, "the dry run rewrote the plan"

        outcome = launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=config_path, runner=runner, print_func=lambda _t: None
        )
        assert outcome.changed, outcome.message
        assert "migrated" in outcome.message, outcome.message
        stored = json.loads(plan_path.read_text(encoding="utf-8"))
        for field in dropped:
            assert field in stored, field
        defaults = plan_field_defaults()
        assert stored["role_control_sudoers_name"] == plan.role_control_sudoers_name
        assert stored["tenant_control_sudoers_name"] == plan.tenant_control_sudoers_name
        assert stored["control_user"] == defaults["control_user"]
        assert stored["role_accounts"] == list(defaults["role_accounts"])

        # Idempotent: a second run has nothing left to migrate and nothing to write.
        again = launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=config_path, runner=runner, print_func=lambda _t: None
        )
        assert not again.changed, again.message
        assert "already current" in again.message, again.message
        assert "migrated" not in again.message, again.message


def privileged_migration(owner: str) -> None:
    """The root branch: the mirror is staged from root's own baseline."""
    project = "porter"
    identity = pwd.getpwnam(owner)
    assert os.geteuid() == 0 and identity.pw_uid != 0
    with tempfile.TemporaryDirectory(prefix="plan-migration-root.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        provision, config_path, plan = _fixture_tenant(root, owner, project=project)
        privileged_root = root / "etc-switchyard"
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(privileged_root)
        subprocess.run(["chown", "-R", f"{owner}:{owner}", str(provision)], check=True)
        config = launcher.load_project_config(project, config_path)
        plan_path = provision / "plan.json"
        mirror = privileged_root / project

        def runner(args, **kwargs):
            if args[0] == "chown":
                return subprocess.run(args, **kwargs)
            return subprocess.CompletedProcess(args, 0, "", "")

        def refresh(**kwargs):
            said: list[str] = []
            outcome = launcher.refresh_generated_project_runtime_artifacts(
                config, config_path=config_path, runner=runner, print_func=said.append, **kwargs
            )
            return outcome, said

        write_legacy_plan(plan_path)
        legacy = plan_path.read_text(encoding="utf-8")

        # Dry run at root reports the migration and stages nothing.
        outcome, _said = refresh(dry_run=True)
        assert "migrated" in outcome.message, outcome.message
        assert "role_control_sudoers_name" in outcome.message, outcome.message
        assert not mirror.exists(), "the dry run staged a privileged mirror"
        assert plan_path.read_text(encoding="utf-8") == legacy, "the dry run rewrote the plan"

        outcome, _said = refresh()
        assert outcome.changed, outcome.message
        assert (mirror / "plan.json").stat().st_uid == 0
        # Root's install carries the regenerated names, from its own baseline.
        assert (mirror / plan.role_control_sudoers_name).is_file(), sorted(
            path.name for path in mirror.iterdir()
        )
        baseline = json.loads((mirror / "plan.json").read_text(encoding="utf-8"))
        assert baseline["role_control_sudoers_name"] == plan.role_control_sudoers_name
        assert baseline["tenant_control_sudoers_name"] == plan.tenant_control_sudoers_name
        assert baseline["role_accounts"], "root's baseline regenerates the role table"

        # Idempotent at root too.
        again, _said = refresh()
        assert not again.changed, again.message
        assert "already current" in again.message, again.message

        # An unsafe override in a legacy document is refused, not migrated in.
        # Root's baseline already exists here, so the mirror is rebuilt from
        # scratch for each field to make the refusal the observation.
        pristine = plan_path.read_text(encoding="utf-8")

        def rebuild() -> None:
            if mirror.exists():
                shutil.rmtree(mirror)

        def refuses(field: str, value: object) -> None:
            rebuild()
            stored = json.loads(pristine)
            stored[field] = value
            plan_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            outcome, _said = refresh()
            assert "cannot establish a root-owned baseline" in outcome.message, (field, outcome.message)
            assert field in outcome.message, (field, outcome.message)
            assert not mirror.exists(), field

        refuses("role_control_sudoers_name", "00-porter-anything")
        refuses("tenant_control_sudoers_name", "00-porter-anything")
        refuses("control_user", "root")

        # And a role-account table the document invents is ignored rather than
        # installed: the migration never turns a tenant claim into authority.
        rebuild()
        stored = json.loads(pristine)
        stored["role_accounts"] = [["director", "root"], ["main", "porter-smuggled"]]
        plan_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        outcome, said = refresh()
        assert outcome.changed, outcome.message
        unit = (mirror / plan.board_unit).read_text(encoding="utf-8")
        declared = [line for line in unit.splitlines() if "TICKET_BOARD_ROLE_ACCOUNTS" in line]
        assert declared, unit
        assert "director=root" not in declared[0], declared
        assert "porter-smuggled" not in declared[0], declared
        for pair in declared[0].split("=", 2)[2].split(","):
            role, account = pair.split("=")
            assert account == f"{project}-{role}", pair


def main() -> int:
    test_the_shape_that_failed_is_the_shape_under_test()
    test_the_migration_is_derived_rather_than_hardcoded()
    test_a_document_that_is_not_a_plan_is_still_refused()
    unprivileged_migration()
    owner = pwd.getpwuid(os.geteuid()).pw_name
    command = [sys.executable, str(Path(__file__).resolve()), "--root-child", owner]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    else:
        command[-1] = "www-data"
    subprocess.run(command, check=True)
    print("team_launcher_plan_migration_test: ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--root-child":
        privileged_migration(sys.argv[2])
    else:
        raise SystemExit(main())
