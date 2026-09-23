#!/usr/bin/env python3
"""`switchyard new` finds the CLI the person running it already has.

Fresh-host reproduction during onboarding: `which claude` resolved in the
operator's session, and `switchyard new` still said

    claude is not installed anywhere this host can reach: not host-wide, and
    not for the account running this command

so the `[p]` offer was missing, the default became `[l]`, and the path prompt
came up blank. `switchyard new` requires sudo, and the caller lookup asked
`shutil.which` with no PATH -- which is root's `secure_path`, not the human's
(SYRD-236).

These cases drive the shipped lookup, not a stand-in for it: no `which` seam is
injected, and the only thing given to the code is which account it is acting
for. Every home is a temporary directory; the invoking account's real home is
never read or written.
"""

from __future__ import annotations

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
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher as launcher  # noqa: E402
from standalone_test_runner import run_module_tests  # noqa: E402

ME = pwd.getpwuid(os.geteuid())
SUPPORTED = ("claude", "codex", "agy", "hermes")


class Sandbox:
    """A home that is not this account's real one, and a PATH that is root's."""

    def __init__(self, *, source: str = "sudo") -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd236."))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.account = launcher.InvokingAccount(
            user=ME.pw_name, uid=ME.pw_uid, gid=ME.pw_gid, home=self.home, source=source
        )
        # Stands for root's secure_path: a real PATH with nothing of the
        # operator's on it. An empty directory rather than the host's own /usr/bin,
        # so a CLI this machine happens to have installed cannot answer for the code.
        self.sanitized = self.tmp / "sanitized"
        self.sanitized.mkdir()
        self._path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.sanitized)
        # This host may really have one of these CLIs installed host-wide, and
        # the host-wide half of the classification would then answer first. The
        # sandbox owns both halves, so every case describes the code.
        self._base_path = launcher.DEFAULT_PANE_BASE_PATH
        (self.tmp / "host-wide").mkdir()
        launcher.DEFAULT_PANE_BASE_PATH = str(self.tmp / "host-wide")
        # No process of this account's is visible either, so a case says what
        # the home directories answer. The process-tree read has its own case.
        self._proc_root = launcher.PROC_ROOT
        launcher.PROC_ROOT = self.tmp / "no-proc"

    def install(self, name: str, *, where: str = ".local/bin", mode: int = 0o755,
                body: str = "#!/bin/sh\nexit 0\n") -> Path:
        directory = self.home / where
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(body, encoding="utf-8")
        path.chmod(mode)
        return path

    def host_wide(self, name: str = "") -> Path:
        """The base PATH every pane is given, for this case."""
        directory = self.tmp / "host-wide"
        if not name:
            return directory
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def close(self) -> None:
        os.environ["PATH"] = self._path
        launcher.DEFAULT_PANE_BASE_PATH = self._base_path
        launcher.PROC_ROOT = self._proc_root
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_a_sudo_crossed_invocation_finds_the_operators_own_executable() -> None:
    """The reproduction: root's PATH has nothing, and the operator's home does."""
    box = Sandbox()
    try:
        installed = box.install("claude")
        assert shutil.which("claude", path=os.environ["PATH"]) != str(installed)
        verdict = launcher.classify_agent_cli("claude", account=box.account)
        assert verdict.scope == launcher.AGENT_CLI_SCOPE_CALLER_ONLY, verdict
        assert verdict.caller_path == str(installed), verdict
        assert verdict.caller_user == ME.pw_name, verdict
    finally:
        box.close()


def test_every_supported_cli_is_found_the_same_way() -> None:
    for cli in SUPPORTED:
        box = Sandbox()
        try:
            binary = launcher.agent_cli_binary(cli)
            installed = box.install(binary)
            verdict = launcher.classify_agent_cli(cli, account=box.account)
            assert verdict.caller_path == str(installed), (cli, verdict)
        finally:
            box.close()


def test_a_toolchain_directory_nobody_could_guess_is_still_searched() -> None:
    """A node version manager puts it behind a directory named for a version."""
    box = Sandbox()
    try:
        installed = box.install("claude", where=".nvm/versions/node/v22.9.0/bin")
        verdict = launcher.classify_agent_cli("claude", account=box.account)
        assert verdict.caller_path == str(installed), verdict
    finally:
        box.close()


def test_an_ordinary_unelevated_invocation_uses_the_callers_own_path() -> None:
    """No sudo: this process IS the operator, and its PATH is the answer."""
    box = Sandbox(source="self")
    try:
        installed = box.install("claude", where="opt/tools")
        os.environ["PATH"] = f"{installed.parent}:{box.sanitized}"
        verdict = launcher.classify_agent_cli("claude", account=box.account)
        assert verdict.caller_path == str(installed), verdict
        assert str(installed.parent) in verdict.caller_search_path, verdict
    finally:
        box.close()


def test_a_host_wide_copy_still_wins_and_is_never_called_caller_only() -> None:
    box = Sandbox()
    try:
        everyones = box.host_wide("claude")
        box.install("claude")
        verdict = launcher.classify_agent_cli("claude", account=box.account)
        assert verdict.scope == launcher.AGENT_CLI_SCOPE_HOST_WIDE, verdict
        assert verdict.host_wide_path == str(everyones), verdict
    finally:
        box.close()


def test_a_name_that_is_only_a_shell_alias_is_not_an_executable() -> None:
    """An alias or function is shell text in a startup file, not a file to promote."""
    box = Sandbox()
    try:
        (box.home / ".bashrc").write_text(
            "alias claude='/opt/vendor/claude --verbose'\n"
            "claude() { /opt/vendor/claude \"$@\"; }\n",
            encoding="utf-8",
        )
        verdict = launcher.classify_agent_cli("claude", account=box.account)
        assert verdict.scope == launcher.AGENT_CLI_SCOPE_ABSENT, verdict
        assert verdict.caller_path == "", verdict
    finally:
        box.close()


def test_a_file_that_is_not_executable_is_not_offered() -> None:
    box = Sandbox()
    try:
        box.install("claude", mode=0o644)
        assert launcher.classify_agent_cli("claude", account=box.account).caller_path == ""
        # And a directory with the right name is not an executable either.
        box2 = Sandbox()
        try:
            (box2.home / ".local" / "bin" / "claude").mkdir(parents=True)
            assert launcher.classify_agent_cli("claude", account=box2.account).caller_path == ""
        finally:
            box2.close()
    finally:
        box.close()


def test_a_dangling_symlink_is_not_an_executable() -> None:
    box = Sandbox()
    try:
        directory = box.home / ".local" / "bin"
        directory.mkdir(parents=True)
        (directory / "claude").symlink_to(box.tmp / "went-away")
        assert launcher.classify_agent_cli("claude", account=box.account).caller_path == ""
    finally:
        box.close()


def test_a_symlinked_install_is_offered_by_its_canonical_path() -> None:
    box = Sandbox()
    try:
        real = box.install("claude-2.1.0", where=".claude/local")
        directory = box.home / ".local" / "bin"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "claude").symlink_to(real)
        verdict = launcher.classify_agent_cli("claude", account=box.account)
        assert verdict.caller_path == str(real), verdict
    finally:
        box.close()


def test_an_executable_the_account_cannot_run_is_not_offered() -> None:
    """Root's access() says yes to anything; the bits say who may run it."""
    box = Sandbox()
    try:
        installed = box.install("claude")
        installed.chmod(0o644)
        assert launcher.classify_agent_cli("claude", account=box.account).caller_path == ""
        installed.chmod(0o700)
        assert launcher.classify_agent_cli("claude", account=box.account).caller_path == str(installed)
        # A directory on the way that the account cannot enter hides it again.
        installed.parent.chmod(0o600)
        try:
            assert launcher.classify_agent_cli("claude", account=box.account).caller_path == ""
        finally:
            installed.parent.chmod(0o755)
    finally:
        box.close()


def test_nothing_found_says_which_account_and_which_directories_were_checked() -> None:
    box = Sandbox()
    try:
        verdict = launcher.classify_agent_cli("claude", account=box.account)
        said = launcher.agent_cli_scope_explanation(verdict, "porter-owner")
        assert "not installed anywhere this host can reach" in said, said
        assert ME.pw_name in said, said
        assert str(box.home / ".local/bin") in said, said
    finally:
        box.close()


def test_discovery_reads_and_writes_nothing_in_the_home_it_searches() -> None:
    box = Sandbox()
    try:
        box.install("claude")
        before = sorted(str(p.relative_to(box.home)) for p in box.home.rglob("*"))
        first = launcher.classify_agent_cli("claude", account=box.account)
        second = launcher.classify_agent_cli("claude", account=box.account)
        assert first == second, (first, second)
        after = sorted(str(p.relative_to(box.home)) for p in box.home.rglob("*"))
        assert after == before, (before, after)
    finally:
        box.close()


class Prompt:
    """One scripted operator, and everything the command said to them."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.said: list[str] = []
        self.asked: list[str] = []

    def input(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    @property
    def text(self) -> str:
        return "\n".join(self.said)


def _promoted(box: Sandbox, answers: list[str]) -> tuple[Prompt, list[tuple[str, str]]]:
    prompt = Prompt(answers)
    promotions: list[tuple[str, str]] = []

    def promoter(cli, source, **_kwargs):
        promotions.append((cli, str(source)))
        return launcher.AgentCliAvailability(
            cli=cli, scope=launcher.AGENT_CLI_SCOPE_HOST_WIDE,
            host_wide_path=f"/usr/local/bin/{cli}",
        )

    launcher.require_agent_clis_for_new_tenant(
        (("main", "claude"),), owner_user="porter-owner", account=box.account,
        input_func=prompt.input, print_func=prompt.said.append, promoter=promoter,
    )
    return prompt, promotions


def test_pressing_enter_promotes_the_executable_that_was_found() -> None:
    """The acceptance: no `which`, no pasted path, one keystroke."""
    box = Sandbox()
    try:
        installed = box.install("claude")
        prompt, promotions = _promoted(box, [""])
        assert f"[p] promote {installed}" in prompt.text, prompt.text
        assert prompt.asked and prompt.asked[0].startswith("Choice [p/l/s/a] (p)"), prompt.asked
        assert promotions == [("claude", str(installed))], promotions
    finally:
        box.close()


def test_choosing_the_local_option_prefills_what_was_found() -> None:
    box = Sandbox()
    try:
        installed = box.install("claude")
        prompt, promotions = _promoted(box, ["l", ""])
        path_prompt = [line for line in prompt.asked if line.startswith("Path to a claude")]
        assert path_prompt and f"[{installed}]" in path_prompt[0], prompt.asked
        assert promotions == [("claude", str(installed))], promotions
    finally:
        box.close()


def test_typing_another_path_still_overrides_the_prefill() -> None:
    box = Sandbox()
    try:
        box.install("claude")
        alternate = box.install("claude", where="opt/alternate")
        prompt, promotions = _promoted(box, ["l", str(alternate)])
        assert promotions == [("claude", str(alternate))], promotions
    finally:
        box.close()


def test_nothing_found_still_asks_and_still_aborts_before_anything_exists() -> None:
    box = Sandbox()
    try:
        prompt = Prompt(["a"])
        raised = ""
        try:
            launcher.require_agent_clis_for_new_tenant(
                (("main", "claude"),), owner_user="porter-owner", account=box.account,
                input_func=prompt.input, print_func=prompt.said.append,
                promoter=lambda *a, **k: None,
            )
        except launcher.AgentCliUnavailable as exc:
            raised = str(exc)
        assert "aborted before creating anything" in raised, raised
        assert "[p] promote" not in prompt.text, prompt.text
        assert ME.pw_name in prompt.text, prompt.text
    finally:
        box.close()


def test_a_second_run_after_promotion_offers_nothing_and_changes_nothing() -> None:
    """Idempotent: once it is host-wide, the gate has no question to ask."""
    box = Sandbox()
    try:
        box.host_wide("claude")
        box.install("claude")
        prompt = Prompt([])
        selection = launcher.require_agent_clis_for_new_tenant(
            (("main", "claude"),), owner_user="porter-owner", account=box.account,
            input_func=prompt.input, print_func=prompt.said.append,
            promoter=lambda *a, **k: None,
        )
        assert selection == (("main", "claude"),), selection
        assert prompt.asked == [], prompt.asked
        assert "host-wide" in prompt.text, prompt.text
    finally:
        box.close()


def test_the_invoking_account_comes_from_the_elevation_not_from_a_flag() -> None:
    mine = launcher.invoking_account(environ={})
    assert mine.user == ME.pw_name and mine.is_this_process, mine
    # A claimed name that the elevation did not record is not consulted.
    claimed = launcher.invoking_account(environ={"SWITCHYARD_CALLER": "somebody"})
    assert claimed.user == ME.pw_name and claimed.is_this_process, claimed
    sudoed = launcher.invoking_account(environ={"SUDO_USER": ME.pw_name})
    assert sudoed.user == ME.pw_name, sudoed
    assert sudoed.home == Path(ME.pw_dir), sudoed
    unknown = launcher.invoking_account(environ={"SUDO_USER": "no-such-account-syrd236"})
    assert unknown.user == ME.pw_name and unknown.is_this_process, unknown


def test_the_callers_own_path_is_read_from_their_process_not_from_a_shell() -> None:
    """The PATH sudo replaced is still on the host: in the caller's own process.

    Built as a /proc rather than taken from one, because the shape only occurs
    when this process is root and the caller is not, which a suite cannot be.
    What is exercised is the walk itself: the ancestor chain, whose uid stops
    it, and that it READS an environment rather than running a shell.
    """
    box = Sandbox()
    try:
        wanted = box.install("claude", where="opt/from-their-path")
        proc = box.tmp / "proc"
        me = os.getpid()
        # This process is root; its parent is the sudo it was started from;
        # that one's parent is the operator's shell, which carries the PATH.
        shell_pid, sudo_pid = 4242, 4243
        for pid, uid, ppid, path in (
            (me, 0, sudo_pid, str(box.sanitized)),
            (sudo_pid, 0, shell_pid, str(box.sanitized)),
            (shell_pid, box.account.uid, 1, f"{wanted.parent}:{box.sanitized}"),
        ):
            entry = proc / str(pid)
            entry.mkdir(parents=True)
            (entry / "status").write_text(
                f"Name:\tthing\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nPPid:\t{ppid}\n",
                encoding="utf-8",
            )
            (entry / "environ").write_bytes(
                b"LANG=C\0PATH=" + path.encode() + b"\0HOME=/root\0"
            )
        searched = launcher.caller_command_search_path(box.account, proc_root=proc)
        assert str(wanted.parent) in searched.split(os.pathsep), searched
        found = launcher.caller_executable("claude", account=box.account, search_path=searched)
        assert found == str(wanted), found
        # A tree with no process of that account's is simply no answer, and the
        # standard per-user directories still are one.
        empty = launcher.caller_command_search_path(box.account, proc_root=box.tmp / "no-proc")
        assert str(wanted.parent) not in empty.split(os.pathsep), empty
        assert str(box.home / ".local/bin") in empty.split(os.pathsep), empty
    finally:
        box.close()


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"caller_cli_discovery_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
