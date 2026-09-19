#!/usr/bin/env python3
"""A fresh tenant must not need its own copy of the agent CLI.

Live on a ZorinOS VM: provisioning a fresh `test` tenant reached the first-run
manifest and reported both selected CLIs missing for `test-agent`, printing the
vendor installers as the remedy. Those install into the account that runs them --
the desktop operator's -- not the owner's, so following the instruction exactly
leaves the tenant no better off. And a per-owner install is the wrong unit
anyway: the next tenant needs its own.

The decision has to happen BEFORE anything is created, because at that moment the
owner has no home, no PATH and no files, so only a host-wide executable can serve
it. That is also the only kind the tenant after this one gets for free (SYRD-210).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from standalone_test_runner import run_module_tests
from scripts import team_launcher
from scripts.team_launcher import (
    AGENT_CLI_POLICY_INSTALL_HOST_WIDE,
    AGENT_CLI_POLICY_REQUIRE_HOST_WIDE,
    AGENT_CLI_SCOPE_ABSENT,
    AGENT_CLI_SCOPE_CALLER_ONLY,
    AGENT_CLI_SCOPE_HOST_WIDE,
    AgentCliUnavailable,
    classify_agent_cli,
    require_agent_clis_for_new_tenant,
)

PANE_PATH = team_launcher.DEFAULT_PANE_BASE_PATH


def which_for(*, host_wide: tuple[str, ...] = (), caller_only: tuple[str, ...] = ()):
    """A `which` that can tell the pane PATH from the caller's own.

    The distinction is the whole classification: a CLI in the operator's home is
    on THEIR PATH and on no pane's.
    """

    def _which(binary: str, path: str | None = None):
        if path == PANE_PATH:
            return f"/usr/local/bin/{binary}" if binary in host_wide else None
        if binary in host_wide:
            return f"/usr/local/bin/{binary}"
        if binary in caller_only:
            return f"/home/eric/.local/bin/{binary}"
        return None

    return _which


class Recorder:
    def __init__(self, answers: list[str] | None = None) -> None:
        self.lines: list[str] = []
        self.answers = list(answers or [])
        self.installed: list[str] = []
        self.prompts: list[str] = []

    def print(self, line: str) -> None:
        self.lines.append(line)

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else "a"

    def installer(self, cli: str, **_kwargs):
        self.installed.append(cli)
        return team_launcher.AgentCliAvailability(
            cli=cli, scope=AGENT_CLI_SCOPE_HOST_WIDE, host_wide_path=f"/usr/local/bin/{cli}"
        )

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def test_classification_separates_host_wide_from_the_operators_own_copy() -> None:
    host = which_for(host_wide=("claude",), caller_only=("codex",))
    assert classify_agent_cli("claude", which=host).scope == AGENT_CLI_SCOPE_HOST_WIDE
    assert classify_agent_cli("codex", which=host).scope == AGENT_CLI_SCOPE_CALLER_ONLY
    assert classify_agent_cli("agy", which=host).scope == AGENT_CLI_SCOPE_ABSENT


def test_all_host_wide_provisions_unchanged_and_says_where() -> None:
    rec = Recorder()
    selection = (("main", "claude"), ("ops", "codex"))
    out = require_agent_clis_for_new_tenant(
        selection,
        owner_user="test-agent",
        which=which_for(host_wide=("claude", "codex")),
        input_func=rec.input,
        print_func=rec.print,
    )
    assert out == selection, out
    assert "/usr/local/bin/claude" in rec.text and "/usr/local/bin/codex" in rec.text
    assert rec.prompts == [], "a fully host-wide selection must ask nothing"


def test_a_caller_only_cli_is_refused_and_never_printed_as_a_bare_installer() -> None:
    """The live failure. The operator's own copy does not serve the new owner."""
    rec = Recorder(answers=["a"])
    try:
        require_agent_clis_for_new_tenant(
            (("main", "codex"),),
            owner_user="test-agent",
            which=which_for(host_wide=("claude",), caller_only=("codex",)),
            input_func=rec.input,
            print_func=rec.print,
        )
    except AgentCliUnavailable as exc:
        assert "aborted before creating anything" in str(exc), exc
    else:
        raise AssertionError("a caller-only CLI must not provision silently")

    assert "/home/eric/.local/bin/codex" in rec.text
    assert "reachable" in rec.text and "only by the account running this command" in rec.text
    assert "curl -fsSL" not in rec.text, (
        "printing the vendor installer here is the original defect: run as printed it "
        "installs for the operator, not the owner"
    )


def test_absent_cli_is_refused_with_both_scopes_named() -> None:
    rec = Recorder(answers=["a"])
    try:
        require_agent_clis_for_new_tenant(
            (("main", "claude"),),
            owner_user="test-agent",
            which=which_for(),
            input_func=rec.input,
            print_func=rec.print,
        )
    except AgentCliUnavailable:
        pass
    else:
        raise AssertionError("an absent CLI must not provision")
    assert "not installed anywhere this host can reach" in rec.text


def test_choosing_a_replacement_switches_only_the_affected_roles() -> None:
    rec = Recorder(answers=["s", "claude"])
    out = require_agent_clis_for_new_tenant(
        (("main", "codex"), ("ops", "codex"), ("audit", "claude")),
        owner_user="test-agent",
        which=which_for(host_wide=("claude",), caller_only=("codex",)),
        input_func=rec.input,
        print_func=rec.print,
    )
    assert out == (("main", "claude"), ("ops", "claude"), ("audit", "claude")), out
    assert rec.installed == [], "switching CLIs must install nothing"


def test_choosing_install_installs_once_and_keeps_the_selection() -> None:
    rec = Recorder(answers=["i"])
    selection = (("main", "codex"), ("ops", "codex"))
    out = require_agent_clis_for_new_tenant(
        selection,
        owner_user="test-agent",
        which=which_for(host_wide=("claude",), caller_only=("codex",)),
        input_func=rec.input,
        print_func=rec.print,
        installer=rec.installer,
    )
    assert out == selection, out
    assert rec.installed == ["codex"], rec.installed


def test_abort_creates_nothing_and_says_so() -> None:
    rec = Recorder(answers=["a"])
    try:
        require_agent_clis_for_new_tenant(
            (("main", "codex"),),
            owner_user="test-agent",
            which=which_for(caller_only=("codex",)),
            input_func=rec.input,
            print_func=rec.print,
            installer=rec.installer,
        )
    except AgentCliUnavailable as exc:
        assert "no tenant residue" in str(exc), exc
    else:
        raise AssertionError("abort must raise")
    assert rec.installed == []


def test_unattended_without_a_declared_policy_fails_before_any_mutation() -> None:
    """Never hang on a prompt, and never guess: say what to declare."""
    rec = Recorder()
    try:
        require_agent_clis_for_new_tenant(
            (("main", "codex"),),
            owner_user="test-agent",
            interactive=False,
            which=which_for(caller_only=("codex",)),
            input_func=rec.input,
            print_func=rec.print,
            installer=rec.installer,
        )
    except AgentCliUnavailable as exc:
        message = str(exc)
        assert "nothing has been created yet" in message, message
        assert "--agent-cli-policy install-host-wide" in message, message
    else:
        raise AssertionError("an unattended run must refuse rather than choose")
    assert rec.prompts == [], "an unattended run must never prompt"
    assert rec.installed == []


def test_unattended_require_host_wide_refuses() -> None:
    rec = Recorder()
    try:
        require_agent_clis_for_new_tenant(
            (("main", "codex"),),
            owner_user="test-agent",
            interactive=False,
            policy=AGENT_CLI_POLICY_REQUIRE_HOST_WIDE,
            which=which_for(caller_only=("codex",)),
            print_func=rec.print,
            installer=rec.installer,
        )
    except AgentCliUnavailable:
        pass
    else:
        raise AssertionError("require-host-wide must refuse an absent CLI")
    assert rec.installed == []


def test_unattended_install_host_wide_installs_without_prompting() -> None:
    rec = Recorder()
    out = require_agent_clis_for_new_tenant(
        (("main", "codex"),),
        owner_user="test-agent",
        interactive=False,
        policy=AGENT_CLI_POLICY_INSTALL_HOST_WIDE,
        which=which_for(caller_only=("codex",)),
        print_func=rec.print,
        installer=rec.installer,
    )
    assert out == (("main", "codex"),)
    assert rec.installed == ["codex"]
    assert rec.prompts == []


def test_a_host_wide_install_serves_the_next_tenant_with_no_further_install() -> None:
    """The reusability requirement, as two consecutive fresh owners."""
    installed: list[str] = []
    host_wide = {"claude"}

    def which(binary: str, path: str | None = None):
        if binary in host_wide:
            return f"/usr/local/bin/{binary}"
        if path == PANE_PATH:
            return None
        return f"/home/eric/.local/bin/{binary}" if binary == "codex" else None

    def installer(cli: str, **_kwargs):
        installed.append(cli)
        host_wide.add(cli)
        return team_launcher.AgentCliAvailability(
            cli=cli, scope=AGENT_CLI_SCOPE_HOST_WIDE, host_wide_path=f"/usr/local/bin/{cli}"
        )

    first = Recorder(answers=["i"])
    require_agent_clis_for_new_tenant(
        (("main", "codex"),), owner_user="alpha-agent",
        which=which, input_func=first.input, print_func=first.print, installer=installer,
    )
    second = Recorder()
    out = require_agent_clis_for_new_tenant(
        (("main", "codex"),), owner_user="beta-agent",
        which=which, input_func=second.input, print_func=second.print, installer=installer,
    )
    assert out == (("main", "codex"),)
    assert installed == ["codex"], f"the second tenant reinstalled it: {installed}"
    assert second.prompts == [], "the second tenant must not be asked anything"


def test_switchyard_refuses_to_run_a_vendor_installer_itself() -> None:
    """The conflict this ticket runs into, pinned as behaviour.

    SYRD-210 asks for "a supported host-wide installation". The only installers
    these vendors publish are `curl | sh`, and host-wide means running one as
    ROOT, during provisioning, from the network. PGU-904 removed CLI
    installation from this module for that reason and guards it structurally.

    So choosing [i] with no supported mechanism refuses -- before anything is
    created -- and says exactly what to do. It does not quietly do it.
    """
    rec = Recorder(answers=["i"])
    try:
        require_agent_clis_for_new_tenant(
            (("main", "codex"),),
            owner_user="test-agent",
            which=which_for(caller_only=("codex",)),
            input_func=rec.input,
            print_func=rec.print,
        )
    except AgentCliUnavailable as exc:
        message = str(exc)
        assert "will not run" in message and "as root" in message, message
        assert "nothing has been created yet" in message, message
    else:
        raise AssertionError("choosing install must not silently run a vendor script")


def test_the_printed_instruction_is_scoped_and_carries_no_credentials() -> None:
    """The original defect was an UNQUALIFIED vendor command. This one says where."""
    instruction = team_launcher.host_wide_install_instruction("claude")
    assert "/usr/local/bin" in instruction, instruction
    assert "root" in instruction, instruction
    assert "throwaway HOME" in instruction, instruction
    assert "credentials stay in the owner account" in instruction, instruction
    assert team_launcher.AGENT_CLI_INSTALL_COMMANDS["claude"] not in instruction, (
        "repeating the bare vendor line is what installed into the wrong account"
    )


def test_an_unknown_cli_still_gets_a_scoped_instruction() -> None:
    instruction = team_launcher.host_wide_install_instruction("some-future-cli")
    assert "/usr/local/bin" in instruction and "root" in instruction, instruction


class OwnerProbe:
    """Stands in for the owner account, answering `command -v` and `--version`."""

    def __init__(self, resolved: dict[str, str], versions: dict[str, str] | None = None) -> None:
        self.resolved = resolved
        self.versions = versions or {}
        self.asked: list[str] = []

    def __call__(self, args, **_kwargs):
        import subprocess as sp

        joined = " ".join(str(a) for a in args)
        self.asked.append(joined)
        for binary, path in self.resolved.items():
            if f"command -v {binary}" in joined:
                return sp.CompletedProcess(args, 0, stdout=f"{path}\n", stderr="")
            if joined.rstrip().endswith(f"{binary} --version"):
                version = self.versions.get(binary, "")
                code = 0 if version else 1
                return sp.CompletedProcess(args, code, stdout=f"{version}\n", stderr="")
        return sp.CompletedProcess(args, 1, stdout="", stderr="not found")


def test_the_owner_account_reports_the_exact_path_and_version() -> None:
    rec = Recorder()
    probe = OwnerProbe({"claude": "/usr/local/bin/claude"}, {"claude": "claude 1.2.3"})
    results = team_launcher.verify_agent_clis_for_owner(
        (("main", "claude"), ("ops", "claude")),
        owner_user="test-agent",
        owner_home=Path("/home/test-agent"),
        runner=probe,
        print_func=rec.print,
    )
    assert [r.cli for r in results] == ["claude"], "one verification per distinct CLI"
    assert results[0].path == "/usr/local/bin/claude"
    assert results[0].version == "claude 1.2.3"
    assert "/usr/local/bin/claude" in rec.text and "claude 1.2.3" in rec.text


def test_a_cli_the_owner_cannot_resolve_is_reported_with_the_scoped_remedy() -> None:
    rec = Recorder()
    probe = OwnerProbe({})
    results = team_launcher.verify_agent_clis_for_owner(
        (("main", "codex"),),
        owner_user="test-agent",
        owner_home=Path("/home/test-agent"),
        runner=probe,
        print_func=rec.print,
    )
    assert not results[0].resolved
    assert "cannot resolve codex" in rec.text
    assert "/usr/local/bin" in rec.text, "the remedy must say where it has to end up"
    assert team_launcher.AGENT_CLI_INSTALL_COMMANDS["codex"] not in rec.text


def test_a_missing_version_is_said_rather_than_faked() -> None:
    rec = Recorder()
    probe = OwnerProbe({"codex": "/usr/local/bin/codex"})
    results = team_launcher.verify_agent_clis_for_owner(
        (("main", "codex"),),
        owner_user="test-agent",
        owner_home=Path("/home/test-agent"),
        runner=probe,
        print_func=rec.print,
    )
    assert results[0].resolved and results[0].version == ""
    assert "version not reported" in rec.text


def test_owner_verification_runs_after_the_owner_account_is_created() -> None:
    """My first attempt put this before the account existed, where it could only fail.

    The precheck and this verification ask different questions and belong on
    opposite sides of the account being created; the source order is the only
    place that distinction is visible.
    """
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    gate = source.index("selected_role_clis = require_agent_clis_for_new_tenant(")
    created = source.index("owner_result = _ensure_owner_user_and_project_dir(")
    # Searched from the top, not from `created`: searching forward turns "it
    # moved above the account" into a ValueError about a missing substring,
    # which says nothing about what is actually wrong.
    verified = source.find("    verify_agent_clis_for_owner(")
    assert verified != -1, "the owner verification call is gone entirely"
    assert gate < created, "the precheck must run before the account is created"
    assert created < verified, (
        "owner verification runs before the account is created, where it can only ever "
        "report that nothing resolves"
    )


def test_the_gate_runs_before_the_first_mutation() -> None:
    """Ordering is the fix, and it is invisible in behaviour until it is wrong.

    Asserted against the source because every mutating call below it would have
    to be driven with a real root environment to show it any other way.
    """
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    gate = source.index("selected_role_clis = require_agent_clis_for_new_tenant(")
    for mutator in (
        "_ensure_board_service_user(precheck_plan.service_user",
        "owner_result = _ensure_owner_user_and_project_dir(",
    ):
        assert gate < source.index(mutator, gate - 4000), (
            f"{mutator} runs before the CLI gate; a refusal would strand a partial tenant"
        )


if __name__ == "__main__":
    run_module_tests(globals())
    print("agent_cli_host_wide_test: ok")
