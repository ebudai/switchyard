"""How to install a missing agent CLI, as text a person runs -- never switchyard.

`host_wide_install_instruction` says how to make one CLI host-wide so every
tenant's panes resolve it (the executable alone, in /usr/local/bin, owned by
root, mode 0755, nothing of the installer's configuration or credentials with
it); `_missing_cli_install_clause` is the one-line remedy a missing-CLI report
prints. Both only format text from the vendor's own install command; switchyard
never fetches or runs a vendor installer (PGU-904, SYRD-210).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-446), in their original
order. The launcher imports this module and re-exports both names;
`agent_cli_promotion.py`, `first_run_auth.py`, `first_run_setup.py` and
`worker_pool_command.py` still read them there. The install-command table they
read stays on the launcher and is read through it when they run, so a suite
that rebinds it there still intercepts it. This module imports `team_launcher`
only inside the functions, when they run.
"""

from __future__ import annotations


def host_wide_install_instruction(cli: str) -> str:
    """How to make one CLI host-wide, scoped so it lands where panes look.

    Switchyard does not run this. PGU-904 removed CLI installation from this
    module on purpose, and the reason applies with more force here: the only
    installers these vendors publish are `curl | sh`, and a host-wide variant
    would have to run one as ROOT, during provisioning, from the network. That
    is a supply-chain decision rather than a convenience, and not one to take
    silently while fixing a usability bug (SYRD-210).

    What this does fix is the half that was plainly wrong. The printed remedy
    used to be the bare vendor line, which installs into whichever account runs
    it -- the desktop operator's, never the owner's. This one says where the
    executable has to end up and what must NOT travel with it.
    """
    from scripts import team_launcher as launcher

    command = launcher.AGENT_CLI_INSTALL_COMMANDS.get(cli, "")
    if not command:
        return (
            f"install {cli} with that vendor's own installer, then place the executable in "
            "/usr/local/bin owned by root, mode 0755, so every tenant resolves it"
        )
    return (
        f"install {cli} host-wide: run the vendor installer under a throwaway HOME so nothing "
        "it writes becomes shared, then move only the executable to /usr/local/bin owned by "
        "root, mode 0755. No configuration, token or session file may travel with it -- "
        "credentials stay in the owner account that authenticates"
    )


def _missing_cli_install_clause(cli: str) -> str:
    """How to install one missing CLI, as text the reader runs themselves.

    It used to say "install <cli> for owner user <owner> with: <vendor command>".
    Both halves were wrong together: the vendor command installs for whoever
    runs it, so following it exactly installed into the operator's own account
    and the tenant still could not start -- and doing it per owner is the
    duplicate installation SYRD-210 removed. Live UAT was given this line on a
    resumed tenant (SYRD-211 second kickback).

    What it names now is the host-wide destination, which serves this owner and
    every later one. The vendor command is still text for a person to run;
    switchyard never fetches or runs it (PGU-904).
    """
    from scripts import team_launcher as launcher

    command = launcher.AGENT_CLI_INSTALL_COMMANDS.get(cli, "")
    installer = command or "that vendor's own installer"
    return (
        f"install {cli} host-wide with {installer}, or let switchyard promote a copy you "
        "already have when it offers"
    )
