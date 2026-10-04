#!/usr/bin/env python3
"""SYRD-543: a plan older than `audit_roles` and `operation_allowed_roles` upgrades and tears down.

Measured on otto (2026-10-04): its plan lacked exactly `audit_roles`,
`commit_git_dir` and `operation_allowed_roles`. Upgrade refused it even with
`--commit-git-dir` ("... and no reference plan can be built for it" -- one could
be), and teardown refused it over fields its actions never read.

Every case here is a disposable tenant in a temporary tree, provisioned by the
real `build_plan`/`write_artifacts`/`write_new_project_launcher_artifacts`, its
plan rolled back to otto's shape, and driven through the real upgrade refresh,
the real plan parser and the real `switchyard_teardown_command` -- dry runs
only, every runner a recorder. Nothing reaches a host service, account or
database.
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.teardown_plan import TEARDOWN_PLAN_FIELDS  # noqa: E402
from scripts.ticket_board.project_provision import PLAN_BASELINE_FIELDS, build_plan, write_artifacts  # noqa: E402
from scripts.ticket_board.plan_role_derivation import PLAN_DERIVED_ROLE_FIELDS  # noqa: E402

CHECKS = 0
OTTO_MISSING = ("audit_roles", "commit_git_dir", "operation_allowed_roles")
COMMIT_STORE = "/data/git/otto-fixture.git"
ME = pwd.getpwuid(os.geteuid()).pw_name


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Tenant:
    """A provisioned tenant in a temporary tree, its plan rolled back to a chosen shape."""

    def __init__(self, project: str = "otto", **plan_options) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix=f"syrd543-{project}."))
        self.home_base = self.tmp / "home"
        self.project = project
        self.plan = build_plan(project=project, project_name=project.title(), owner_user=ME,
                               owner_home=self.home_base / ME, source_repo=ROOT, **plan_options)
        self.provision = self.tmp / "provision"
        self.provision.mkdir()
        (self.tmp / "repository").mkdir()
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(self.tmp / "etc-switchyard")
        write_artifacts(self.plan, self.provision, enable_owner_linger=False)
        self.config_path = launcher.write_new_project_launcher_artifacts(
            self.plan, self.provision, repository=self.tmp / "repository", print_func=lambda _t: None)
        self.plan_path = self.provision / "plan.json"

    def roll_back(self, *missing: str, **override) -> dict:
        stored = json.loads(self.plan_path.read_text(encoding="utf-8"))
        for name in missing:
            del stored[name]
        stored.update(override)
        self.plan_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return stored

    def parse(self, **supplied) -> tuple[object, list[str]]:
        """The parsed plan and what was migrated; a refusal here is a failed check, never a crash."""
        migrated: list[str] = []
        try:
            return launcher._project_board_provision_from_json(self.plan_path, migrated=migrated, supplied=supplied or None), migrated
        except SystemExit as exc:
            check(False, f"{self.project}: the plan was refused: {exc}")
            raise

    def refused(self, **supplied) -> str:
        try:
            launcher._project_board_provision_from_json(self.plan_path, supplied=supplied or None)
        except SystemExit as exc:
            return str(exc)
        return ""

    def refresh(self, **kwargs):
        config = launcher.load_project_config(self.project, self.config_path)
        return launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=self.config_path, print_func=lambda _t: None,
            runner=lambda args, **_kw: subprocess.CompletedProcess(args, 0, "", ""), **kwargs)

    def teardown_dry_run(self) -> tuple[int | str, list[str], list[list[str]]]:
        calls: list[list[str]] = []
        lines: list[str] = []

        def runner(args, **_kw):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "0\n", "")

        try:
            code: int | str = launcher.switchyard_teardown_command(
                self.project, dry_run=True, config_dir=self.config_path.parent, registry_dir=self.tmp / "registry",
                home_base=self.home_base, runner=runner, print_func=lines.append)
        except SystemExit as exc:
            code = str(exc)
        return code, lines, calls


def test_otto_shaped_plan_upgrades_with_its_commit_store_and_reports_the_migration() -> None:
    tenant = Tenant()
    original = {name: list(getattr(tenant.plan, name)) for name in PLAN_DERIVED_ROLE_FIELDS}
    tenant.roll_back(*OTTO_MISSING)

    # Without the operator's commit store it is still refused, for that reason alone -- provenance stays strict.
    outcome = tenant.refresh(dry_run=True)
    check(not outcome.changed and "missing provision field 'commit_git_dir'" in outcome.message
          and "--commit-git-dir" in outcome.message and "audit_roles" not in outcome.message,
          f"upgrade without --commit-git-dir: the commit-store refusal, nothing about the role fields: {outcome.message}")

    # With it: past plan parsing, both role fields reported as migrated, and a dry run writes nothing.
    before = tenant.plan_path.read_bytes()
    outcome = tenant.refresh(dry_run=True, commit_git_dir=COMMIT_STORE)
    check(not outcome.changed and "cannot be refreshed" not in outcome.message and "missing provision field" not in outcome.message
          and "audit_roles" in outcome.message and "operation_allowed_roles" in outcome.message
          and "commit_git_dir (from the command line)" in outcome.message,
          f"upgrade --commit-git-dir --dry-run: parsed, and audit_roles and operation_allowed_roles reported as migrated: {outcome.message}")
    check(tenant.plan_path.read_bytes() == before, "and the dry run did not rewrite the plan")

    # Applied: the values provisioning gave this tenant -- no operation permission broadened.
    outcome = tenant.refresh(commit_git_dir=COMMIT_STORE)
    stored = json.loads(tenant.plan_path.read_text(encoding="utf-8"))
    check(outcome.changed and {name: stored[name] for name in PLAN_DERIVED_ROLE_FIELDS} == original
          and stored["audit_roles"] == ["audit"] and stored["operation_allowed_roles"] == []
          and stored["commit_git_dir"] == COMMIT_STORE,
          f"applied, the plan records exactly what provisioning gave it: {[stored.get(n) for n in sorted(PLAN_DERIVED_ROLE_FIELDS)]}")
    again = tenant.refresh(dry_run=True)
    check("missing provision field" not in again.message and "cannot be refreshed" not in again.message,
          f"and a later plain upgrade needs nothing more: {again.message}")
    check(set(OTTO_MISSING) - PLAN_BASELINE_FIELDS == set(PLAN_DERIVED_ROLE_FIELDS)
          and "commit_git_dir" in PLAN_BASELINE_FIELDS,
          "the two role fields are derived, not baseline; the commit store stays baseline (provenance)")


def test_derived_from_the_tenants_own_roles_or_not_at_all() -> None:
    # Custom implementers and no designer: derived from THOSE roles, equal to what provisioning gave.
    custom = Tenant("ottoc", implementer_roles=("alpha", "beta"), include_designer=False)
    original = {name: getattr(custom.plan, name) for name in PLAN_DERIVED_ROLE_FIELDS}
    custom.roll_back(*OTTO_MISSING)
    plan, migrated = custom.parse(commit_git_dir=COMMIT_STORE)
    check({name: getattr(plan, name) for name in PLAN_DERIVED_ROLE_FIELDS} == original
          and {"audit_roles", "operation_allowed_roles"} <= set(migrated),
          f"custom implementers, no designer: the tenant's own audit and operation roles: {migrated}")
    no_audit = Tenant("ottoa", include_audit=False)
    no_audit.roll_back(*OTTO_MISSING)
    plan, _ = no_audit.parse(commit_git_dir=COMMIT_STORE)
    check(plan.audit_roles == () and plan.operation_allowed_roles == (),
          f"a tenant provisioned without audit gets no auditor invented: {plan.audit_roles}")

    # Role policy provisioning would not reproduce from the recorded roles is refused, never guessed.
    for label, options, why in (
        ("custom audit roles", {"audit_roles": ("qa",)}, "recorded caller_roles"),
        ("a VCS close role", {"vcs_close_role": "closer", "implementer_roles": ("main", "closer")}, "recorded caller_roles"),
    ):
        tenant = Tenant("ottor", **options)
        check(tenant.plan.operation_allowed_roles != (), f"{label}: the fixture has a real operation map to lose")
        tenant.roll_back(*OTTO_MISSING)
        refusal = tenant.refused(commit_git_dir=COMMIT_STORE)
        check(refusal.startswith(f"switchyard: {tenant.plan_path} is missing provision field 'audit_roles': ")
              and why in refusal and "cannot be derived" in refusal and "no reference plan" not in refusal,
              f"{label}: refused with the real reason, not filled with a policy it never had: {refusal}")


def test_true_identity_is_still_refused_and_says_why() -> None:
    for name in ("board_root", "database"):
        tenant = Tenant()
        tenant.roll_back(name, *OTTO_MISSING)
        refusal = tenant.refused(commit_git_dir=COMMIT_STORE)
        check(refusal == f"switchyard: {tenant.plan_path} is missing provision field {name!r}: it records this "
              "tenant's own identity or provenance, which is never derived; re-provision the project",
              f"missing {name}: refused as identity, never derived, and no false 'no reference plan': {refusal}")
    # When no reference plan can be built, that -- and only then -- is what it says.
    check("no reference plan could be built" in launcher.unresolved_plan_field_reason("audit_roles", {}, None)
          and "no reference plan could be built" in launcher.unresolved_plan_field_reason("role_control_sudoers_name", {}, None)
          and "no reference plan" not in launcher.unresolved_plan_field_reason("listener_log", {}, None),
          "no reference plan: said only when there is none")


def test_otto_shaped_teardown_dry_run_lists_its_actions() -> None:
    tenant = Tenant()
    tenant.roll_back(*OTTO_MISSING)
    code, lines, calls = tenant.teardown_dry_run()
    actions = [line for line in lines if line.strip()[:1].isdigit() and ". " in line]
    check(code == 0 and len(actions) == 13 and "switchyard: dry-run only; no changes made" in lines
          and any("drop PostgreSQL database otto_ticket_board" in line for line in actions)
          and any(f"remove board release root {tenant.home_base / ME / 'otto-ticketboard-live'}" in line for line in actions),
          f"teardown --dry-run of the otto-shaped tenant lists its actions, with no plan-field refusal: {code} {lines}")
    check(calls == [["psql", "-XAt", "postgresql:///postgres?host=/var/run/postgresql", "-c",
                     "SELECT 1 FROM pg_database WHERE datname = 'otto_ticket_board'"]],
          f"and only the read-only ticket-count probe runs: {calls}")


def test_teardown_validates_what_it_removes_by() -> None:
    for label, change, expected in (
        ("missing database", {"drop": "database"}, "records no usable 'database', which teardown removes by: it records this tenant's own identity"),
        ("board root outside the owner's home", {"board_root": "/etc"}, "board_root '/etc' is not a normalized directory inside the owner's home"),
        ("board root that climbs out", {"board_root": "@HOME/../../etc"}, "is not a normalized directory inside the owner's home"),
        ("the owner's home itself", {"board_root": "@HOME"}, "is not a normalized directory inside the owner's home"),
        ("another project's plan", {"project": "someone"}, "it is the plan of 'someone', not of registered project 'otto'"),
        ("a unit name with a path", {"board_unit": "../../etc/passwd"}, "board_unit '../../etc/passwd' is not a bare file name"),
        ("an odd database name", {"database": "x; DROP"}, "database 'x; DROP' is not a plain database name"),
    ):
        tenant = Tenant()
        home = str(tenant.home_base / ME)
        override = {k: (v.replace("@HOME", home) if isinstance(v, str) else v) for k, v in change.items() if k != "drop"}
        tenant.roll_back(*OTTO_MISSING, *([change["drop"]] if "drop" in change else []), **override)
        code, lines, calls = tenant.teardown_dry_run()
        check(isinstance(code, str) and expected in code and not lines and not calls,
              f"{label}: refused before anything is planned or probed: {code!r} {lines} {calls}")


def test_teardown_requires_exactly_what_its_actions_read() -> None:
    """The fields teardown requires are the plan attributes its own code reads -- no more, no fewer."""
    source = ast.parse((ROOT / "scripts" / "project_teardown.py").read_text(encoding="utf-8"))
    read = {node.attr for node in ast.walk(source)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "plan"}
    check(read == set(TEARDOWN_PLAN_FIELDS), f"teardown reads {sorted(read)}; it requires {sorted(TEARDOWN_PLAN_FIELDS)}")
    check(not set(OTTO_MISSING) & set(TEARDOWN_PLAN_FIELDS), "and none of the three otto lacked")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"legacy_plan_roles_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
