"""A tenant's publication boundary: taking it away on the ordinary upgrade, and giving it to an existing tenant.

- `remove_tenant_publication_boundary` removes the publication sudo rule a
  tenant was given -- root only, honouring a dry run, and checking the rule is
  gone rather than trusting the command -- and never the root-owned key and
  grant it leaves behind.
- `install_tenant_publication_boundary` installs the boundary a fresh tenant is
  given, for the owner and the GitHub identity the tenant's recorded plan
  selects, and reports what is still pending as a problem.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-395), in their original
order. The launcher imports this module and re-exports both names, so the
upgrade's tooling phase that reads the removal through it, and every suite
that rebinds it there, reach the same objects. The launcher facilities the
installation reads -- the current user, a user's home, the recorded plan and
the privileged provision root -- are read from `team_launcher` when it runs, as
they were, so a patch on the launcher still intercepts. The provisioning and
publication helpers are still imported inside each function when it runs. The
`runner` and `print_func` defaults are bound when each function is defined, as
they were. The standard-library names are this module's own imports, the same
objects. `ProjectConfig` is imported for annotations only. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def remove_tenant_publication_boundary(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    dry_run: bool = False,
    sudoers_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Take away the publication hop this tenant was given, on the ordinary upgrade.

    The User restored the project account's write access to the forge, so an
    implementer publishes by pushing and there is nothing for a root-owned
    publisher to do (SYRD-123). Leaving it installed would leave a NOPASSWD rule
    to a privileged program in place while the documentation says there is no
    privileged gate, and the first of those two is the one that would still be
    true.

    What goes: the sudo rule, and -- through the retirement list in
    `role_tooling_staging_commands` -- the staged copies of the programs it
    named. What stays: the root-owned key and grant under /etc/switchyard, which
    no role can read and nothing now runs. Deleting a credential is not
    reversible and this decision has already been reversed once, so it is not
    this upgrade's to make; with the rule gone there is no path to it.
    """
    from scripts.ticket_board.project_provision import publish_sudoers_path

    sudoers_path = Path(publish_sudoers_path(config.project, root=sudoers_root))
    if dry_run:
        if sudoers_path.exists():
            print_func(f"switchyard: would remove {config.project}'s publication sudo rule {sudoers_path}")
        return []
    if os.geteuid() != 0:
        return [f"{sudoers_path} can only be removed by root"]
    if not sudoers_path.exists():
        return []
    removed = runner(
        ["rm", "-f", str(sudoers_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if getattr(removed, "returncode", 1) != 0:
        detail = (str(getattr(removed, "stderr", "") or "").strip() or "no output")[:300]
        return [f"could not remove {sudoers_path}: {detail}"]
    # Gone, not "the command said so". A grant still on disk is still a grant,
    # and an upgrade that reports it removed is the one way this can be worse
    # than leaving it alone.
    if sudoers_path.exists():
        return [f"{sudoers_path} is still on disk after removing it"]
    print_func(
        f"switchyard: removed {config.project}'s publication sudo rule; implementers publish by "
        "pushing to the project remote and nothing here runs as root"
    )
    return []


def install_tenant_publication_boundary(
    config: ProjectConfig,
    *,
    release,
    publish_remote: str = "",
    config_path: Path | None = None,
    dry_run: bool = False,
    sudoers_root: Path | None = None,
    registration_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Give an existing tenant the publication boundary a fresh one is given.

    `publish_grant_commands()` is reachable from the operator command packet and
    from nowhere else, and the shared-project-account path skips the role-account
    migration that used to carry it, so a tenant that was provisioned before
    SYRD-93 upgrades everything except the one thing that stops every role under
    the shared account from pushing. This is that step, on the ordinary upgrade,
    run by root in this process rather than written out for an operator to
    execute -- a generated file is writable by every role under one account, and
    running it as root would hand them root (SYRD-97).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        publish_sudoers_document,
        publish_sudoers_path,
    )
    from scripts.ticket_board.publication_boundary import (
        install_publication_boundary,
        report_publication_outcome,
    )

    owner_user = config.run_as_user or launcher.current_user_name()
    sudoers_path = str(publish_sudoers_path(config.project, root=sudoers_root))
    # The credential this boundary exists to replace. Named so the report can
    # print the exact bounded check for it, and so its fingerprint keys the
    # evidence: a rotated shared key is not the one a verdict was about.
    from scripts.ticket_board.project_provision import (
        owner_github_key_path,
        resolve_owner_github_identity,
    )

    # Which key this tenant actually publishes with, not the default name. A
    # verdict recorded against the wrong key would be a verdict about a
    # credential nobody uses (SYRD-100 found the same trap from the other side).
    owner_home = launcher.home_dir_for_user(owner_user)
    plan_data = launcher._plan_data_from_config(config, config_path) if config_path else {}
    selected = resolve_owner_github_identity(
        str(owner_home),
        recorded_key_name=str(plan_data.get("owner_github_key_name") or ""),
        recorded_host_alias=str(plan_data.get("owner_github_host_alias") or ""),
    )
    shared_identity = owner_github_key_path(
        str(owner_home), key_name=selected.key_name if selected.resolved else ""
    )
    outcome = install_publication_boundary(
        project=config.project,
        release=release,
        registration_root=registration_root or launcher.switchyard_privileged_provision_root(),
        sudoers_path=sudoers_path,
        sudoers_document=publish_sudoers_document(config.project, owner_user),
        declared_remote=publish_remote,
        shared_identity_file=shared_identity,
        owner_user=owner_user,
        dry_run=dry_run,
        runner=runner,
        print_func=print_func,
    )
    for problem in outcome.problems:
        print_func(f"warning: switchyard: {problem}")
    if not dry_run:
        # After the problems, and driven by what is on disk: a report that runs
        # before the verdict prints artifacts a failed run never reached.
        report_publication_outcome(outcome, project=config.project, print_func=print_func)
    # Pending is not success. It used to reach nobody, so a failed keyscan left
    # the phase recorded done while the boundary was demonstrably unfinished
    # (SYRD-97 review).
    return list(outcome.problems) + [
        f"{path} was not installed, so {config.project}'s publication boundary is incomplete"
        for path in outcome.pending
    ]
