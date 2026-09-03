#!/usr/bin/env python3
"""The missing-CLI messages must hand the user a runnable install command.

Since PGU-904, Switchyard installs no agent CLIs. That makes these messages the
entire contract with someone on a fresh machine: "install claude" restates the
problem, it does not solve it. PGU-905 puts the vendor's own command in the
message -- as text, for the reader to run.

The second half of that contract is that nothing here ever RUNS those commands.
That is checked structurally below rather than by observing one quiet run.
"""

from __future__ import annotations

import ast

from team_launcher_test_helpers import *


CLIS = ("agy", "claude", "codex", "hermes")
# The only functions allowed to touch the vendor command table. Both format
# text; neither can execute anything. Adding a name here is a deliberate act.
COMMAND_TABLE_READERS = {"_missing_cli_install_clause", "_format_missing_cli_launch_failure"}
EXECUTION_MARKERS = ("subprocess", "runner", "os.system", "popen", "check_call", "check_output")


def _report(missing: dict[str, list[str]], owner_user: str = "otto-agent"):
    return team_launcher.FirstRunAuthReport(
        unauthenticated_roles={},
        untrusted_roles=[],
        missing_cli_roles=missing,
        owner_user=owner_user,
    )


def test_every_supported_cli_has_a_verified_install_command() -> None:
    table = team_launcher.AGENT_CLI_INSTALL_COMMANDS

    assert set(table) == set(CLIS), sorted(table)
    # Every CLI Switchyard probes for must have something to say when missing.
    assert set(team_launcher.FIRST_RUN_AUTH_STATUS_COMMANDS) <= set(table)
    for cli, command in table.items():
        assert command.startswith("curl -fsSL https://"), (cli, command)
        # codex ships a POSIX sh installer; the others are bash. Piping a bash
        # script to sh is the kind of quiet breakage a reader would not spot.
        assert command.endswith("| sh") if cli == "codex" else command.endswith("| bash"), (cli, command)


def test_docs_table_and_code_table_cannot_drift_apart() -> None:
    """Two copies of the vendor commands exist on purpose; keep them equal.

    docs/fresh-machine-install.md serves a reader who has not run anything yet;
    AGENT_CLI_INSTALL_COMMANDS serves someone whose launch just stopped. Both
    are legitimate, but silently disagreeing about how to install a CLI is
    worse than either being stale on its own.
    """
    docs = (Path(team_launcher.__file__).resolve().parents[1] / "docs" / "fresh-machine-install.md").read_text(
        encoding="utf-8"
    )
    # The docs render pipes as `\|` inside the markdown table.
    normalized = docs.replace("\\|", "|")
    for cli, command in team_launcher.AGENT_CLI_INSTALL_COMMANDS.items():
        assert command in normalized, (cli, command)


def test_hard_stop_names_the_command_the_owner_user_and_who_runs_it() -> None:
    message = team_launcher._format_missing_cli_launch_failure(
        _report({"claude": ["designer", "director"], "codex": ["ops"]})
    )

    # Still a hard stop with the same opening contract.
    assert "cannot launch panes because required CLI(s) are missing for owner user otto-agent" in message
    assert "claude (roles: designer, director)" in message
    assert "codex (roles: ops)" in message

    # ...now carrying something runnable, per missing CLI and only those.
    assert team_launcher.AGENT_CLI_INSTALL_COMMANDS["claude"] in message
    assert team_launcher.AGENT_CLI_INSTALL_COMMANDS["codex"] in message
    assert team_launcher.AGENT_CLI_INSTALL_COMMANDS["agy"] not in message
    assert team_launcher.AGENT_CLI_INSTALL_COMMANDS["hermes"] not in message

    # ...and the failure people actually hit, preempted.
    assert "panes run as that user" in message
    assert "installed only for the user running switchyard is not found" in message
    assert "switchyard does not install agent CLIs" in message


def test_manifest_and_warning_lines_also_carry_the_command() -> None:
    manifest = team_launcher.FirstRunSetupManifest(
        owner_user="otto-agent",
        login_steps=[],
        folder_trust_steps=[],
        stale_codex_hook_trust=[],
        missing_cli_roles={"agy": ["inspector"]},
    )
    lines = team_launcher._format_first_run_setup_manifest(manifest)
    assert any(team_launcher.AGENT_CLI_INSTALL_COMMANDS["agy"] in line for line in lines), lines
    assert any("installed only for the user running switchyard is not found" in line for line in lines), lines

    output: list[str] = []
    team_launcher.report_first_run_auth_warnings(_report({"agy": ["inspector"]}), print_func=output.append)
    assert len(output) == 1, output
    assert team_launcher.AGENT_CLI_INSTALL_COMMANDS["agy"] in output[0], output
    assert "for owner user otto-agent" in output[0], output


def test_unknown_cli_degrades_to_prose_instead_of_raising() -> None:
    # missing_cli_roles is populated from FIRST_RUN_AUTH_STATUS_COMMANDS today,
    # so this cannot happen yet. It will the first time the two tables diverge,
    # and a KeyError there would take out the whole stop message.
    clause = team_launcher._missing_cli_install_clause("some-future-cli", "otto-agent")
    assert clause == "install some-future-cli for owner user otto-agent with that vendor's own installer"

    message = team_launcher._format_missing_cli_launch_failure(_report({"some-future-cli": ["main"]}))
    assert "see that vendor's own installation documentation" in message


def test_owner_user_is_omitted_cleanly_when_unknown() -> None:
    clause = team_launcher._missing_cli_install_clause("claude", "")
    assert clause == f"install claude with: {team_launcher.AGENT_CLI_INSTALL_COMMANDS['claude']}"

    message = team_launcher._format_missing_cli_launch_failure(_report({"claude": ["main"]}, owner_user=""))
    assert "for owner user" not in message
    assert "panes run as the project's owner user" in message


def test_nothing_can_execute_a_vendor_install_command() -> None:
    """Structural: the table is read only by text formatters.

    PGU-904 removed CLI installation on purpose. Hardcoding the vendor commands
    to print them puts a loaded gun in the module, and the obvious "improvement"
    is to run them. This fails if the table is ever read anywhere that could.
    """
    source = Path(team_launcher.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    readers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.dump(node)
        if "AGENT_CLI_INSTALL_COMMANDS" not in body:
            continue
        readers.add(node.name)
        lowered = ast.unparse(node).lower()
        for marker in EXECUTION_MARKERS:
            assert marker not in lowered, f"{node.name} reads the install table and mentions {marker}"

    assert readers == COMMAND_TABLE_READERS, sorted(readers)

    # And each command string exists exactly once in the module: its entry in
    # the table. A second literal would mean it had been copied somewhere the
    # AST check above does not cover, such as a hand-built argument list.
    for command in team_launcher.AGENT_CLI_INSTALL_COMMANDS.values():
        assert source.count(command) == 1, (command, source.count(command))


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_missing_cli_install_hint_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
