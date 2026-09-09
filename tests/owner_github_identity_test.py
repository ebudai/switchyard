#!/usr/bin/env python3
"""SYRD-74: a project owner that cannot offer a key cannot publish.

The switchyard-agent account held a perfectly good ED25519 key under a
nonstandard filename and nothing selected it for github.com, so git offered no
key at all and publication failed with `Permission denied (publickey)` -- which
reads like a missing key and was a missing *selection*.

These cover the selection, the modes and ownership around it, the
non-interactive readiness probe, and the operator action a human is given when
GitHub does not have the matching public half. Nothing here reads, writes or
asserts anything about private key material.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *
from scripts.ticket_board.project_provision import (
    GITHUB_IDENTITY_BEGIN,
    GITHUB_IDENTITY_END,
    compose_ssh_config,
    github_identity_block,
    owner_github_identity_commands,
    owner_github_key_path,
)

OWNER = team_launcher.current_user_name()
KEY_NAME = "id_ed25519_ebudai_switchyard"


def test_a_named_key_is_selected_for_github() -> None:
    """The live account's key was named, and nothing pointed at it."""
    block = github_identity_block(
        "/home/agent", key_name=KEY_NAME, host="github.com", host_alias="github-switchyard"
    )
    assert block.startswith(GITHUB_IDENTITY_BEGIN), block
    assert block.rstrip().endswith(GITHUB_IDENTITY_END), block
    assert f"IdentityFile /home/agent/.ssh/{KEY_NAME}" in block, block
    # Without this an agent holding other keys offers those first and GitHub
    # answers for whichever account it recognises.
    assert block.count("IdentitiesOnly yes") == 2, block
    assert "Host github.com" in block and "Host github-switchyard" in block, block
    # The default is the ordinary name, so a project that never named one still
    # gets a selection.
    assert owner_github_key_path("/home/agent") == "/home/agent/.ssh/id_ed25519"


def test_the_managed_block_replaces_itself_and_keeps_everything_else() -> None:
    """An operator's own stanzas have to survive every upgrade."""
    theirs = "Host internal\n    HostName 10.0.0.1\n    User ops\n"
    first = compose_ssh_config(theirs, github_identity_block("/home/agent", key_name=KEY_NAME))
    assert theirs.strip() in first, first
    # First, not appended: ssh takes the first value it obtains for a keyword,
    # so a managed identity behind somebody's `Host *` would not apply.
    assert first.startswith(GITHUB_IDENTITY_BEGIN), first
    second = compose_ssh_config(first, github_identity_block("/home/agent", key_name=KEY_NAME))
    assert second == first, second
    assert second.count(GITHUB_IDENTITY_BEGIN) == 1, second
    # A run interrupted between the markers leaves a begin with no end; the
    # remainder was this block's, and is replaced rather than kept.
    torn = f"{GITHUB_IDENTITY_BEGIN}\nHost github.com\n    IdentityFile /wrong\n"
    repaired = compose_ssh_config(
        theirs + torn, github_identity_block("/home/agent", key_name=KEY_NAME)
    )
    assert "/wrong" not in repaired, repaired
    assert theirs.strip() in repaired, repaired
    assert repaired.count(GITHUB_IDENTITY_BEGIN) == 1, repaired


def _sudo_shim(tmp: Path) -> Path:
    """`sudo` for a test that is not root: run the command, drop what needs root."""
    binaries = tmp / "bin"
    binaries.mkdir(exist_ok=True)
    shim = binaries / "sudo"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv[:1] == ['-u']:\n"
        "    argv = argv[2:]\n"
        "kept = []\n"
        "index = 0\n"
        "while index < len(argv):\n"
        "    if argv[index] in ('-o', '-g') and index + 1 < len(argv):\n"
        "        index += 2\n"
        "        continue\n"
        "    kept.append(argv[index])\n"
        "    index += 1\n"
        "if not kept or kept[0] == 'chown':\n"
        "    raise SystemExit(0)\n"
        "os.execvp(kept[0], kept)\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return binaries


def _apply(commands: list[str], *, binaries: Path) -> None:
    subprocess.run(
        ["bash", "-c", "\n".join(["set -euo pipefail", *commands])],
        check=True,
        env={**os.environ, "PATH": f"{binaries}:{os.environ.get('PATH', '')}"},
        capture_output=True,
    )


def test_the_rendered_commands_really_produce_that_configuration() -> None:
    """The shell is the implementation; the composer above is its specification."""
    with tempfile.TemporaryDirectory(prefix="github-identity.") as raw:
        tmp = Path(raw)
        home = tmp / "home"
        home.mkdir()
        theirs = "Host internal\n    HostName 10.0.0.1\n"
        (home / ".ssh").mkdir(mode=0o700)
        (home / ".ssh" / "config").write_text(theirs, encoding="utf-8")
        binaries = _sudo_shim(tmp)
        commands = owner_github_identity_commands(OWNER, str(home), key_name=KEY_NAME)
        _apply(commands, binaries=binaries)

        key = Path(owner_github_key_path(str(home), key_name=KEY_NAME))
        config = home / ".ssh" / "config"
        assert key.is_file() and (key.with_name(key.name + ".pub")).is_file()
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE(key.with_name(key.name + ".pub").stat().st_mode) == 0o644
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert config.read_text(encoding="utf-8") == compose_ssh_config(
            theirs, github_identity_block(str(home), key_name=KEY_NAME)
        )

        # Re-runnable, and the key it already had is the key it still has.
        before = key.read_bytes()
        _apply(commands, binaries=binaries)
        assert key.read_bytes() == before
        assert config.read_text(encoding="utf-8") == compose_ssh_config(
            theirs, github_identity_block(str(home), key_name=KEY_NAME)
        )
        # Nothing printed the private half, and nothing copied it.
        rendered = "\n".join(commands)
        assert "cat" not in rendered and "PRIVATE KEY" not in rendered, rendered


def _identity_home(tmp: Path) -> Path:
    home = tmp / "owner-home"
    (home / ".ssh").mkdir(parents=True, mode=0o700)
    key = home / ".ssh" / KEY_NAME
    key.write_text("not a real key\n", encoding="utf-8")
    key.chmod(0o600)
    public = key.with_name(key.name + ".pub")
    public.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI0000 owner switchyard\n", encoding="utf-8")
    public.chmod(0o644)
    config = home / ".ssh" / "config"
    config.write_text(github_identity_block(str(home), key_name=KEY_NAME), encoding="utf-8")
    config.chmod(0o600)
    return home


def _probe(answer: str, *, code: int = 1):
    def runner(argv, **kwargs):
        joined = " ".join(str(part) for part in argv)
        if "ssh-keygen" in joined:
            return subprocess.CompletedProcess(list(argv), 0, "256 SHA256:abc owner (ED25519)\n", "")
        if " -T " in f" {joined} ":
            return subprocess.CompletedProcess(list(argv), code, answer, "")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    return runner


def test_a_ready_identity_is_reported_ready() -> None:
    with tempfile.TemporaryDirectory(prefix="github-ready.") as raw:
        home = _identity_home(Path(raw))
        status = team_launcher.github_identity_status(
            OWNER, home, key_name=KEY_NAME,
            runner=_probe("Hi ebudai! You've successfully authenticated, but GitHub does not provide shell access."),
        )
        assert status.problems == (), status.problems
        assert status.authenticated is True, status
        assert status.ready is True, status
        assert team_launcher.github_identity_remedy(status) == ""
        # The public half is carried; nothing else about the key is.
        assert status.public_key.startswith("ssh-ed25519 "), status.public_key
        assert "PRIVATE" not in status.public_key


def test_github_not_having_the_key_names_the_exact_operator_action() -> None:
    """The failure the incident produced, and what a human does about it."""
    with tempfile.TemporaryDirectory(prefix="github-denied.") as raw:
        home = _identity_home(Path(raw))
        status = team_launcher.github_identity_status(
            OWNER, home, key_name=KEY_NAME,
            runner=_probe("git@github.com: Permission denied (publickey).", code=255),
        )
        assert status.problems == (), status.problems
        assert status.authenticated is False, status
        remedy = team_launcher.github_identity_remedy(status, project="otto")
        assert "Permission denied (publickey)" in remedy, remedy
        assert "https://github.com/settings/keys" in remedy, remedy
        assert status.public_key in remedy, remedy
        assert "SHA256:abc" in remedy, remedy
        assert "sudo switchyard upgrade otto" in remedy, remedy
        assert "PRIVATE" not in remedy


def test_a_missing_selection_is_named_as_the_reason() -> None:
    """The live shape: a good key, and nothing pointing git at it."""
    with tempfile.TemporaryDirectory(prefix="github-unselected.") as raw:
        home = _identity_home(Path(raw))
        (home / ".ssh" / "config").write_text("Host internal\n", encoding="utf-8")
        (home / ".ssh" / "config").chmod(0o600)
        status = team_launcher.github_identity_status(
            OWNER, home, key_name=KEY_NAME,
            runner=_probe("git@github.com: Permission denied (publickey).", code=255),
        )
        assert any("selects no managed identity" in problem for problem in status.problems), status
        remedy = team_launcher.github_identity_remedy(status, project="otto")
        assert "re-runnable" in remedy, remedy


def test_bad_ownership_or_modes_are_reported_rather_than_assumed() -> None:
    with tempfile.TemporaryDirectory(prefix="github-modes.") as raw:
        home = _identity_home(Path(raw))
        (home / ".ssh" / KEY_NAME).chmod(0o644)
        (home / ".ssh").chmod(0o755)
        status = team_launcher.github_identity_status(
            OWNER, home, key_name=KEY_NAME, runner=_probe("Permission denied (publickey)."),
        )
        assert any("mode 0644" in problem for problem in status.problems), status.problems
        assert any("mode 0755" in problem for problem in status.problems), status.problems
        assert status.ready is False


def test_the_probe_is_non_interactive_and_bounded() -> None:
    """A readiness check that can prompt or hang is one that hangs a launch."""
    seen: list[list[str]] = []

    def recording(argv, **kwargs):
        words = [str(part) for part in argv]
        seen.append(words)
        if "-T" in words:
            # The probe itself is the one that must not sit there.
            assert kwargs.get("timeout"), kwargs
        return subprocess.CompletedProcess(list(argv), 1, "successfully authenticated", "")

    with tempfile.TemporaryDirectory(prefix="github-probe.") as raw:
        home = _identity_home(Path(raw))
        team_launcher.github_identity_status(OWNER, home, key_name=KEY_NAME, runner=recording)
    probe = next(argv for argv in seen if "-T" in argv)
    assert "BatchMode=yes" in probe, probe
    assert "ConnectTimeout=5" in probe, probe
    assert "git@github.com" in probe, probe


def test_an_unready_identity_is_a_first_run_warning() -> None:
    """It is reported where the other readiness gates are, before work is routed."""
    with tempfile.TemporaryDirectory(prefix="github-report.") as raw:
        home = _identity_home(Path(raw))
        status = team_launcher.github_identity_status(
            OWNER, home, key_name=KEY_NAME,
            runner=_probe("git@github.com: Permission denied (publickey).", code=255),
        )
        report = team_launcher.FirstRunAuthReport(
            unauthenticated_roles={}, untrusted_roles=[], owner_user=OWNER, github_identity=status
        )
        assert report.has_warnings is True
        printed: list[str] = []
        team_launcher.report_first_run_auth_warnings(report, print_func=printed.append)
        assert any("cannot publish to GitHub" in line for line in printed), printed
        assert any("settings/keys" in line for line in printed), printed

        ready = team_launcher.github_identity_status(
            OWNER, home, key_name=KEY_NAME, runner=_probe("successfully authenticated"),
        )
        quiet = team_launcher.FirstRunAuthReport(
            unauthenticated_roles={}, untrusted_roles=[], owner_user=OWNER, github_identity=ready
        )
        assert quiet.has_warnings is False
        printed.clear()
        team_launcher.report_first_run_auth_warnings(quiet, print_func=printed.append)
        assert printed == [], printed


def test_provisioning_emits_the_identity_before_anything_publishes() -> None:
    from scripts.ticket_board.project_provision import build_plan, render_operator_commands

    plan = build_plan(
        project="otto",
        owner_user="otto-agent",
        owner_home=Path("/home/otto-agent"),
        board_root=Path("/home/otto-agent/otto-ticketboard-live"),
        source_repo=Path("/opt/switchyard/current"),
    )
    rendered = render_operator_commands(plan)
    assert GITHUB_IDENTITY_BEGIN in rendered, rendered[-2000:]
    assert "ssh-keygen -t ed25519" in rendered, rendered[-2000:]
    assert "IdentitiesOnly yes" in rendered, rendered[-2000:]
    # Root creates the directory; the key is generated by the owner, never by
    # root, so nothing root-owned lands in that account's .ssh.
    assert "sudo -u 'otto-agent' ssh-keygen" in rendered, rendered[-2000:]


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("owner_github_identity_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
