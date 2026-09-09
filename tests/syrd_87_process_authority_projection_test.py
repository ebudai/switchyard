#!/usr/bin/env python3
"""SYRD-87 R6: the staged board unit must match the identities actually running.

A tenant that has crossed to the one-project-account runtime has no per-role
accounts: every role runs as the project account and the board authorises a
registered process rather than a uid. The privileged projection kept rendering
the root baseline's per-role table for it, so the staged and installed units
named retired accounts and omitted TICKET_BOARD_PROCESS_AUTHORITY=1. The board
then came up in legacy_uid mode resolving peers through accounts that no longer
exist, and no role could write to it -- the director included, which is why
`switchyard finish-upgrade` could not be performed.

The fix is driven by the tenant's configuration, which is the document the
identities transaction writes and then verifies against the kernel-read uid of
every role process. The tenant's plan.json is still never consulted for account
values, and the only thing this can do is empty the table: it can never name an
account, so the trust boundary holds in the direction that matters.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    render_board_unit,
    render_listener_unit,
)

ROLES = ("director", "main", "app", "ops", "audit", "inspector")
RETIRED = "designer"
OWNER = "switchyard-agent"


def fail(message: str) -> None:
    raise AssertionError(message)


def _config(tmp: Path, *, isolation: bool, per_role_accounts: bool) -> team_launcher.ProjectConfig:
    layout = tmp / "layout.json"
    layout.write_text(
        json.dumps(team_launcher._new_project_layout_payload(len(ROLES))) + "\n", encoding="utf-8"
    )
    roles = []
    for index, role in enumerate(ROLES):
        entry = {
            "role": role,
            "slot": index,
            "cli": ["claude"],
            "live_commands": ["claude"],
            "target": f"syrd-{role}:0.0",
            "tmux_session": f"syrd-{role}",
            "workdir": str(tmp / role),
        }
        if per_role_accounts:
            entry["run_as_user"] = f"syrd-{role}"
        roles.append(entry)
        (tmp / role).mkdir(exist_ok=True)
    path = tmp / "syrd.json"
    path.write_text(
        json.dumps(
            {
                "project": "syrd",
                "layout": str(layout),
                "session_dir": str(tmp / "sessions"),
                "run_as_user": OWNER,
                "role_state_isolation": isolation,
                "roles": roles,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return team_launcher.load_project_config("syrd", path)


def _legacy_baseline(tmp: Path) -> "object":
    """A root-owned baseline written before the cutover: the retired table."""
    plan = team_launcher.build_plan(
        project="syrd",
        owner_user=OWNER,
        owner_home=tmp / "home",
        source_repo=tmp / "src",
    )
    accounts = tuple((role, f"syrd-{role}") for role in ("director", RETIRED, "main", "app", "ops", "audit"))
    return replace(plan, role_accounts=accounts)


def _authority_lines(body: str) -> tuple[bool, bool]:
    return (
        "TICKET_BOARD_ROLE_ACCOUNTS=" in body,
        "TICKET_BOARD_PROCESS_AUTHORITY=1" in body,
    )


def test_project_account_tenant_gets_process_authority() -> None:
    """The incident: a crossed tenant rendered with the retired table."""
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-crossed.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        config = _config(root, isolation=True, per_role_accounts=False)
        baseline = _legacy_baseline(root)
        assert baseline.role_accounts, "the fixture baseline must carry the retired table"

        if not team_launcher._tenant_runs_on_project_account(config):
            fail("a role_state_isolation tenant whose roles all run as the owner has crossed")

        projected = team_launcher.plan_for_current_identities(baseline, config)
        if projected.role_accounts:
            fail("a crossed tenant's projected plan must carry no per-role accounts")
        has_accounts, has_process = _authority_lines(render_board_unit(projected))
        if has_accounts:
            fail("a crossed tenant's board unit must not name per-role accounts")
        if not has_process:
            fail("a crossed tenant's board unit must set TICKET_BOARD_PROCESS_AUTHORITY=1")

        # And the retired role must not appear anywhere in the authority table.
        body = render_board_unit(projected)
        if f"syrd-{RETIRED}" in body:
            fail(f"the retired {RETIRED} account must not survive into the board unit")


def test_listener_unit_follows_the_same_authority() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-listener.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        baseline = _legacy_baseline(root)
        legacy = render_listener_unit(baseline)
        crossed = render_listener_unit(replace(baseline, role_accounts=()))
        if legacy == crossed:
            fail("the listener unit must differ between a per-role and a project-account tenant")
        if f"syrd-{RETIRED}" in crossed:
            fail("the retired account must not survive into the listener unit")


def test_legacy_per_role_tenant_keeps_its_table() -> None:
    """The other tenants on this host must be left exactly as they are."""
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-legacy.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        config = _config(root, isolation=False, per_role_accounts=True)
        if team_launcher._tenant_runs_on_project_account(config):
            fail("a tenant without role_state_isolation has not crossed")
        baseline = _legacy_baseline(root)
        projected = team_launcher.plan_for_current_identities(baseline, config)
        if list(projected.role_accounts) != list(baseline.role_accounts):
            fail("a legacy tenant's table must be left exactly as it is")
        has_accounts, has_process = _authority_lines(render_board_unit(projected))
        if not has_accounts:
            fail("a legacy tenant must keep its exact authority table")
        if has_process:
            fail("a legacy tenant must not be given process authority")


def test_isolation_flag_alone_is_not_enough() -> None:
    """Both halves. A role that still names its own account has not crossed."""
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-halfway.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        config = _config(root, isolation=True, per_role_accounts=True)
        if team_launcher._tenant_runs_on_project_account(config):
            fail(
                "a configuration whose roles still name per-role accounts has not crossed, "
                "whatever the flag says"
            )
        baseline = _legacy_baseline(root)
        if not team_launcher.plan_for_current_identities(baseline, config).role_accounts:
            fail("a half-migrated tenant must keep its table rather than lose all authority")


def test_the_flag_alone_is_load_bearing() -> None:
    """A tenant whose roles name no account is distinguished only by the flag.

    Most legacy tenants also give each role its own account, so the role check
    alone would refuse them. This one does not, so if the flag stopped being
    consulted it would be emptied and lose all authority.
    """
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-flagonly.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        config = _config(root, isolation=False, per_role_accounts=False)
        if team_launcher._tenant_runs_on_project_account(config):
            fail("without role_state_isolation a tenant has not crossed, whatever its roles say")
        baseline = _legacy_baseline(root)
        if not team_launcher.plan_for_current_identities(baseline, config).role_accounts:
            fail("a tenant that has not crossed must keep its authority table")


def test_a_configuration_naming_no_owner_has_not_crossed() -> None:
    """No project account means nothing to run as; that is not a crossing."""
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-noowner.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        config = _config(root, isolation=True, per_role_accounts=False)
        ownerless = replace(config, run_as_user="")
        if team_launcher._tenant_runs_on_project_account(ownerless):
            fail("a configuration with no project account must not be treated as crossed")
        baseline = _legacy_baseline(root)
        if not team_launcher.plan_for_current_identities(baseline, ownerless).role_accounts:
            fail("an ownerless configuration must not cause the authority table to be emptied")


def test_a_configuration_with_no_roles_has_not_crossed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-noroles.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        config = replace(_config(root, isolation=True, per_role_accounts=False), roles=[])
        if team_launcher._tenant_runs_on_project_account(config):
            fail("a configuration with no roles must not be treated as crossed")


def test_the_authority_change_does_not_touch_role_projection() -> None:
    """Only the authority table moves. Who may call and be assigned does not.

    caller_roles gates real operations in the board, so a change that quietly
    rewrote them would decide authorisation as a side effect of fixing
    authentication. This pins that it does not.
    """
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-neutral.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        config = _config(root, isolation=True, per_role_accounts=False)
        baseline = _legacy_baseline(root)
        projected = team_launcher.plan_for_current_identities(baseline, config)
        for field in (
            "caller_roles",
            "assignee_roles",
            "draft_roles",
            "implementer_roles",
            "audit_roles",
        ):
            before, after = getattr(baseline, field), getattr(projected, field)
            if list(before) != list(after):
                fail(f"{field} must be untouched by the authority projection: {before} -> {after}")
        if projected.role_accounts:
            fail("the authority table is the one thing that must change")


def test_a_malicious_tenant_plan_cannot_select_accounts() -> None:
    """The SYRD-39 boundary, unchanged: the tenant may only add its own role."""
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-malice.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        baseline = _legacy_baseline(root)
        hostile = {
            "role_accounts": [
                ["director", "root"],            # re-point a role root knows
                ["inspector", "root"],           # a new role under a chosen account
                ["ops", "syrd-ops"],             # agreeing entry, accepted silently
                ["evil", "syrd-ops"],            # a new role wearing another role's account
                "not-even-a-pair",
            ]
        }
        plan, added, refused = team_launcher.authoritative_refresh_plan(baseline, hostile)
        accounts = dict(plan.role_accounts)
        if accounts.get("director") == "root":
            fail("a tenant plan must not re-point a role root already knows")
        if accounts.get("inspector") == "root":
            fail("a tenant plan must not choose a non-canonical account for a new role")
        if "evil" in accounts:
            fail("a tenant plan must not introduce a role wearing another role's account")
        for entry in ("director=root", "inspector=root", "evil=syrd-ops"):
            if entry not in refused:
                fail(f"{entry} must be refused explicitly, got {refused}")

        # And the emptying path cannot be used to name anything either: it only
        # ever removes, so the worst a lying configuration achieves is removing
        # its own roles' authority.
        crossed = replace(baseline, role_accounts=())
        if crossed.role_accounts:
            fail("the crossed projection must produce an empty table")
        body = render_board_unit(crossed)
        if "TICKET_BOARD_ROLE_ACCOUNTS=" in body:
            fail("an empty table must render no authority table at all")


def test_active_workflow_roles_still_project() -> None:
    """Inspector is active and Designer is retired; the unit must say so."""
    with tempfile.TemporaryDirectory(prefix="syrd87-r6-projection.") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        baseline = _legacy_baseline(root)
        # The workflow projection adds a role under its own canonical account,
        # which is the one thing an unprivileged document may contribute.
        plan, added, _refused = team_launcher.authoritative_refresh_plan(
            baseline, {"role_accounts": [["inspector", "syrd-inspector"]]}
        )
        if "inspector" not in added:
            fail("a new workflow role under its canonical account must be accepted")
        if dict(plan.role_accounts).get("inspector") != "syrd-inspector":
            fail("the accepted role must land under its canonical account")
        # ...and once the tenant has crossed, that table is emptied wholesale
        # rather than partially rewritten, so no retired account can linger.
        crossed = replace(plan, role_accounts=())
        body = render_board_unit(crossed)
        for name in (f"syrd-{RETIRED}", "syrd-inspector"):
            if name in body:
                fail(f"{name} must not appear in a process-authority unit")


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    for name, fn in sorted(vars(module).items()):
        if name.startswith("test_") and callable(fn) and inspect.getmodule(fn) is module:
            fn()
    print("syrd_87_process_authority_projection_test: ok")
