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
    AGENT_CLI_POLICY_PROMOTE_LOCAL,
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

    def promoter(self, cli: str, source, **_kwargs):
        self.installed.append((cli, str(source)))
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
    rec = Recorder(answers=["p"])
    selection = (("main", "codex"), ("ops", "codex"))
    out = require_agent_clis_for_new_tenant(
        selection,
        owner_user="test-agent",
        which=which_for(host_wide=("claude",), caller_only=("codex",)),
        input_func=rec.input,
        print_func=rec.print,
        promoter=rec.promoter,
    )
    assert out == selection, out
    assert [cli for cli, _src in rec.installed] == ["codex"], rec.installed


def test_abort_creates_nothing_and_says_so() -> None:
    rec = Recorder(answers=["a"])
    try:
        require_agent_clis_for_new_tenant(
            (("main", "codex"),),
            owner_user="test-agent",
            which=which_for(caller_only=("codex",)),
            input_func=rec.input,
            print_func=rec.print,
            promoter=rec.promoter,
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
            promoter=rec.promoter,
        )
    except AgentCliUnavailable as exc:
        message = str(exc)
        assert "nothing has been created yet" in message, message
        assert "--agent-cli-policy promote-local" in message, message
        assert "never fetches a vendor installer" in message, message
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
            promoter=rec.promoter,
        )
    except AgentCliUnavailable:
        pass
    else:
        raise AssertionError("require-host-wide must refuse an absent CLI")
    assert rec.installed == []


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

    def promoter(cli: str, source, **_kwargs):
        installed.append(cli)
        host_wide.add(cli)
        return team_launcher.AgentCliAvailability(
            cli=cli, scope=AGENT_CLI_SCOPE_HOST_WIDE, host_wide_path=f"/usr/local/bin/{cli}"
        )

    first = Recorder(answers=["p"])
    require_agent_clis_for_new_tenant(
        (("main", "codex"),), owner_user="alpha-agent",
        which=which, input_func=first.input, print_func=first.print, promoter=promoter,
    )
    second = Recorder()
    out = require_agent_clis_for_new_tenant(
        (("main", "codex"),), owner_user="beta-agent",
        which=which, input_func=second.input, print_func=second.print, promoter=promoter,
    )
    assert out == (("main", "codex"),)
    assert installed == ["codex"], f"the second tenant reinstalled it: {installed}"
    assert second.prompts == [], "the second tenant must not be asked anything"


def test_promote_local_needs_a_source_and_says_which_one() -> None:
    """A policy says what may happen; it cannot say which executable to promote."""
    rec = Recorder()
    try:
        require_agent_clis_for_new_tenant(
            (("main", "codex"),),
            owner_user="test-agent",
            interactive=False,
            policy=AGENT_CLI_POLICY_PROMOTE_LOCAL,
            which=which_for(caller_only=("codex",)),
            print_func=rec.print,
            promoter=rec.promoter,
        )
    except AgentCliUnavailable as exc:
        message = str(exc)
        assert "needs a source for codex" in message, message
        # and it offers the one it already found, rather than making them hunt
        assert "--agent-cli-source codex=/home/eric/.local/bin/codex" in message, message
    else:
        raise AssertionError("promote-local without a source must refuse")
    assert rec.installed == []


def test_unattended_promote_local_uses_the_declared_source() -> None:
    rec = Recorder()
    out = require_agent_clis_for_new_tenant(
        (("main", "codex"),),
        owner_user="test-agent",
        interactive=False,
        policy=AGENT_CLI_POLICY_PROMOTE_LOCAL,
        sources={"codex": "/opt/vendor/codex"},
        which=which_for(caller_only=("codex",)),
        print_func=rec.print,
        promoter=rec.promoter,
    )
    assert out == (("main", "codex"),)
    assert rec.installed == [("codex", "/opt/vendor/codex")], rec.installed
    assert rec.prompts == []


def test_the_detected_executable_is_offered_as_the_default_source() -> None:
    """The Director's refinement: promote what we already found, in one keystroke."""
    rec = Recorder(answers=[""])  # empty answer takes the default
    require_agent_clis_for_new_tenant(
        (("main", "codex"),),
        owner_user="test-agent",
        which=which_for(caller_only=("codex",)),
        input_func=rec.input,
        print_func=rec.print,
        promoter=rec.promoter,
    )
    assert rec.installed == [("codex", "/home/eric/.local/bin/codex")], rec.installed
    assert "promote /home/eric/.local/bin/codex" in rec.text


def test_promotion_copies_the_real_file_and_never_links_into_a_home() -> None:
    """End to end against the filesystem, because this one has to be a copy.

    The usual source is a per-user install in the operator's home. A symlink
    would leave every tenant's PATH pointing into an account they cannot read
    and whose owner can replace the target.
    """
    import os
    import stat
    import subprocess as sp
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home_bin = root / "home" / "eric" / ".local" / "bin"
        home_bin.mkdir(parents=True)
        real = home_bin / "codex-1.4.0"
        real.write_text("#!/bin/sh\necho 'codex 1.4.0'\n", encoding="utf-8")
        real.chmod(0o755)
        link = home_bin / "codex"
        link.symlink_to(real)

        bins = root / "usr" / "local" / "bin"
        chowned: list[tuple[str, int, int]] = []

        def fake_chown(path, uid, gid):
            chowned.append((str(path), uid, gid))

        verdict = team_launcher.promote_agent_cli_host_wide(
            "codex",
            link,
            bin_dir=bins,
            which=lambda binary, path=None: str(bins / binary) if (bins / binary).exists() else None,
            print_func=lambda _line: None,
            chown=fake_chown,
            runner=sp.run,
        )

        installed = bins / "codex"
        assert installed.is_file() and not installed.is_symlink(), "must be a copy, not a link"
        assert installed.read_text() == real.read_text()
        assert stat.S_IMODE(installed.stat().st_mode) == 0o755
        assert chowned and chowned[-1][1:] == (0, 0), "the result must be root-owned"
        assert verdict.serves_a_new_owner
        assert not any(p.name.startswith(".codex.switchyard-promote") for p in bins.iterdir()), (
            "the staging file must not be left behind"
        )


def test_a_source_that_is_not_an_executable_is_refused_by_name() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing = root / "nope"
        for source, expected in (
            (missing, "no codex executable"),
            (root, "is a directory"),
        ):
            try:
                team_launcher.resolve_agent_cli_source("codex", source)
            except team_launcher.AgentCliSourceRejected as exc:
                assert expected in str(exc), (source, exc)
            else:
                raise AssertionError(f"{source} should have been refused")

        plain = root / "not-executable"
        plain.write_text("x", encoding="utf-8")
        plain.chmod(0o644)
        try:
            team_launcher.resolve_agent_cli_source("codex", plain)
        except team_launcher.AgentCliSourceRejected as exc:
            assert "not executable" in str(exc), exc
        else:
            raise AssertionError("a non-executable file should have been refused")


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


# --- a launcher is not a promotable artifact --------------------------------
#
# Live on a fresh host, promoting Hermes: `p` copied
# /home/santiago/.local/bin/hermes, and verification as the future owner
# sbs-agent exited 126 -- the copy still exec'd
# /home/santiago/.hermes/hermes-agent/venv/bin/python, which that account
# cannot traverse. The copy was the pointer, not the runtime.
#
# Two things were wrong. It was offered as the one-keystroke default when its
# first two lines already said it could not serve a tenant; and the copy was
# installed before it was verified, so a host that already had a working
# host-wide CLI lost it to a promotion that then failed.


def a_launcher_in_a_private_home(root: Path, cli: str = "hermes"):
    """The live shape: a launcher whose runtime is inside a 0700 home."""
    import os

    private = root / "home" / "santiago"
    venv = private / f".{cli}/{cli}-agent/venv/bin"
    venv.mkdir(parents=True)
    interpreter = venv / "python"
    interpreter.write_text("#!/bin/sh\necho '" + cli + " 9.9'\n")
    interpreter.chmod(0o755)
    (private / ".local/bin").mkdir(parents=True)
    launcher = private / ".local/bin" / cli
    launcher.write_text(f"#!{interpreter}\n")
    launcher.chmod(0o755)
    # What makes a stranger's exec fail with 126, and what neither the owner
    # nor root can notice by trying it.
    os.chmod(private, 0o700)
    return launcher, interpreter


def test_reachability_is_read_from_the_bits_not_from_this_account() -> None:
    """The account that asks is never the account that has the problem.

    The operator owns the home, and root bypasses the check; the future tenant
    is neither, and does not exist yet to be asked.
    """
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        launcher, interpreter = a_launcher_in_a_private_home(root)
        assert os.access(interpreter, os.X_OK), "this account cannot reach it, so nothing is proven"
        problems = team_launcher.agent_cli_unreachable_dependencies(launcher)
        assert [str(path) for path, _why in problems] == [str(interpreter)], problems
        assert "not searchable by other accounts" in problems[0][1], problems

        # The positive control has to open the WHOLE chain: a temporary
        # directory is 0700 itself, so nothing beneath it is reachable by a
        # stranger either -- which is the check working, not a false alarm.
        for parent in [root, *reversed(interpreter.parents)]:
            if parent == root or parent.is_relative_to(root):
                os.chmod(parent, 0o755)
        assert team_launcher.agent_cli_unreachable_dependencies(launcher) == [], (
            "an executable every account can reach was still called unreachable"
        )


def test_a_wrapper_that_execs_a_private_runtime_is_caught_too() -> None:
    """The other launcher shape, and the more common one.

    `#!/bin/sh` is reachable by everyone, so the shebang says nothing. What a
    tenant cannot reach is the interpreter the wrapper execs on the next line.
    """
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _launcher, interpreter = a_launcher_in_a_private_home(root)
        wrapper = root / "hermes-wrapper"
        wrapper.write_text(f'#!/bin/sh\nexec {interpreter} -m hermes "$@"\n')
        wrapper.chmod(0o755)

        problems = team_launcher.agent_cli_unreachable_dependencies(wrapper)
        assert [str(path) for path, _why in problems] == [str(interpreter)], problems
        # /bin/sh itself is reachable and must not be reported.
        assert not any("/bin/sh" in str(path) for path, _why in problems), problems
        os.chmod(root / "home" / "santiago", 0o700)
        assert team_launcher.agent_cli_source_is_self_contained("hermes", wrapper), (
            "a wrapper around a private runtime was called self-contained"
        )


def test_a_launcher_into_a_private_home_is_not_offered_as_promotable() -> None:
    """Checked before it is offered, not after root has been asked for."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        launcher, interpreter = a_launcher_in_a_private_home(root)

        def which(binary: str, path: str | None = None):
            if path == PANE_PATH:
                return "/usr/local/bin/claude" if binary == "claude" else None
            if binary == "claude":
                return "/usr/local/bin/claude"
            return str(launcher) if binary == "hermes" else None

        rec = Recorder(answers=["a"])
        try:
            require_agent_clis_for_new_tenant(
                (("main", "hermes"),), owner_user="sbs-agent", which=which,
                input_func=rec.input, print_func=rec.print, promoter=rec.promoter,
            )
        except AgentCliUnavailable as exc:
            assert "aborted" in str(exc), exc
        else:
            raise AssertionError("the abort choice did not abort")

        assert "[p] promote" not in rec.text, f"a launcher was offered as promotable: {rec.text}"
        assert str(interpreter) in rec.text, f"the reason does not name what a tenant cannot reach: {rec.text}"
        assert "not a self-contained hermes" in rec.text, rec.text
        assert rec.installed == [], rec.installed
        # And the supported alternatives are still there.
        assert "[l] promote a local executable" in rec.text, rec.text
        assert "[s] switch the affected roles" in rec.text, rec.text
        assert any("[l/s/a]" in prompt for prompt in rec.prompts), rec.prompts


def test_a_resumed_tenant_is_not_offered_a_launcher_either() -> None:
    """The same artifact, the other entry point.

    A resumed tenant has an owner already, so there is no alternative CLI to
    switch to here -- the honest answer is to say why it cannot be promoted and
    leave the host and the running tenant alone.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        launcher, interpreter = a_launcher_in_a_private_home(Path(tmp))

        def which(binary: str, path: str | None = None):
            if path == PANE_PATH:
                return None
            return str(launcher) if binary == "hermes" else None

        rec = Recorder(answers=["p"])  # would accept, if it were offered
        promoted = team_launcher.offer_host_wide_promotion_before_launch(
            "sbs", which=which, input_func=rec.input, print_func=rec.print,
            promoter=rec.promoter,
        )
        assert promoted == [], f"a launcher was promoted for a resumed tenant: {promoted}"
        assert rec.installed == [], rec.installed
        assert "[p] promote" not in rec.text, f"it was offered anyway: {rec.text}"
        assert rec.prompts == [], f"an unanswerable question was asked: {rec.prompts}"
        assert str(interpreter) in rec.text, rec.text
        assert "continues unchanged" in rec.text, rec.text


def test_a_launcher_is_refused_before_sudo_is_asked_for() -> None:
    """Refused locally: no password prompt to learn what its first line says."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        launcher, _interpreter = a_launcher_in_a_private_home(Path(tmp))
        ran: list[list[str]] = []

        def runner(args, **_kwargs):
            ran.append([str(a) for a in args])
            raise AssertionError("sudo was asked for")

        try:
            team_launcher.promote_agent_cli_through_sudo(
                "hermes", launcher, project="sbs", runner=runner, print_func=lambda _t: None,
            )
        except SystemExit as exc:
            assert "not a self-contained hermes" in str(exc), exc
        else:
            raise AssertionError("a launcher was promoted through sudo")
        assert ran == [], f"something was run before the refusal: {ran}"


def test_a_failed_verification_leaves_the_existing_host_wide_copy_alone() -> None:
    """The failure and the damage used to be the same step."""
    import subprocess as sp
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bindir = root / "usr-local-bin"
        bindir.mkdir()
        working = bindir / "codex"
        working.write_text("#!/bin/sh\necho 'the copy every tenant is using'\n")
        working.chmod(0o755)
        before = working.read_bytes()

        candidate = root / "codex-that-cannot-run"
        candidate.write_text("#!/bin/sh\nexit 1\n")
        candidate.chmod(0o755)

        def runner(args, **_kwargs):
            return sp.CompletedProcess(args, 126, "exec: permission denied", "")

        try:
            team_launcher.promote_agent_cli_host_wide(
                "codex", candidate, bin_dir=bindir, runner=runner,
                which=lambda *_a, **_k: str(working), print_func=lambda _t: None,
                chown=lambda *_a: None,
            )
        except SystemExit as exc:
            assert "tenant context" in str(exc), exc
        else:
            raise AssertionError("an executable that cannot run in a tenant context was installed")

        assert working.read_bytes() == before, "a failed promotion replaced the working copy"
        leftovers = [path.name for path in bindir.iterdir() if path.name != "codex"]
        assert leftovers == [], f"a staging file was left behind: {leftovers}"


def test_a_self_contained_executable_is_still_promoted() -> None:
    """The control: the cases above must not pass by refusing everything."""
    import subprocess as sp
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bindir = root / "usr-local-bin"
        bindir.mkdir()
        source = root / "codex"
        source.write_text("#!/bin/sh\necho 'codex 1.2.3'\n")
        source.chmod(0o755)

        verified: list[list[str]] = []

        def runner(args, **_kwargs):
            verified.append([str(a) for a in args])
            return sp.CompletedProcess(args, 0, "codex 1.2.3\n", "")

        said: list[str] = []
        verdict = team_launcher.promote_agent_cli_host_wide(
            "codex", source, bin_dir=bindir, runner=runner,
            which=lambda binary, path=None: str(bindir / binary),
            print_func=said.append, chown=lambda *_a: None,
        )
        assert verdict.serves_a_new_owner, verdict
        assert (bindir / "codex").read_text() == source.read_text()
        assert verified and verified[0][0] != str(bindir / "codex"), (
            f"it was verified after being installed, not before: {verified}"
        )
        assert "codex 1.2.3" in "\n".join(said), said


def _run() -> None:
    """Report an escaping SystemExit against the test that raised it.

    AgentCliUnavailable IS a SystemExit -- correct for the product, since it
    ends the command cleanly -- but an uncaught one in a test ends the whole
    suite printing only its own message, with no indication of which test let it
    out. That cost a confused minute here; it should not cost the next person
    one.
    """
    for name, test in sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    ):
        try:
            test()
        except SystemExit as exc:
            raise AssertionError(
                f"{name} let a SystemExit escape: {str(exc).splitlines()[0]}"
            ) from exc


if __name__ == "__main__":
    _run()
    print("agent_cli_host_wide_test: ok")
