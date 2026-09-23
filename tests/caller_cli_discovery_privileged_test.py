#!/usr/bin/env python3
"""As root, with sudo's PATH, `switchyard new` still finds the operator's CLI.

The reported shape cannot be built unprivileged: it needs this process to be
root while the human who typed the command is somebody else, which is exactly
when `shutil.which` with no PATH answers for the wrong account. On the fresh
host that printed

    claude is not installed anywhere this host can reach

the operator's `which claude` worked and root's `secure_path` had nothing
(SYRD-236).

Runs as root in a user and mount namespace. A scratch directory is bound over
a real account's home for the run, so the account's own files are never read or
written, and the classification is driven with no test seam at all: only
SUDO_USER, the way sudo leaves it.
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


def _candidate_account() -> pwd.struct_passwd:
    """An account whose home can be bound over without hiding this checkout."""
    for entry in sorted(pwd.getpwall(), key=lambda e: e.pw_uid):
        home = Path(entry.pw_dir)
        if not (0 < entry.pw_uid < 65536) or home == Path("/") or not home.is_dir():
            continue
        if home.is_relative_to("/home") or ROOT.is_relative_to(home):
            continue
        if Path(tempfile.gettempdir()).is_relative_to(home):
            continue
        return entry
    raise SystemExit("caller_cli_discovery_privileged_test: no account with a bindable home")


#: Asks the shipped code the one question this suite exists for, as a child so
#: the answer comes from a process whose environment is sudo's.
PROBE = """
import json, os, sys
sys.path.insert(0, os.environ["SYRD236_ROOT"])
from scripts import team_launcher as launcher

cli = os.environ["SYRD236_CLI"]
# Asked first, and with nothing this change added, so the same probe runs
# against the commit this regressed from and answers it there too.
verdict = launcher.classify_agent_cli(cli)
account = getattr(launcher, "invoking_account", lambda: None)()
print(json.dumps({
    "euid": os.geteuid(),
    "account_user": getattr(account, "user", ""),
    "account_uid": getattr(account, "uid", -1),
    "account_home": str(getattr(account, "home", "")),
    "account_source": getattr(account, "source", ""),
    "scope": verdict.scope,
    "caller_path": verdict.caller_path,
    "caller_user": getattr(verdict, "caller_user", ""),
    "explanation": launcher.agent_cli_scope_explanation(verdict, "porter-owner"),
    "host_wide_path": verdict.host_wide_path,
}))
"""


def _cli_this_host_does_not_have() -> tuple[str, str]:
    """A CLI name with no host-wide copy here, and the binary it runs.

    The supported names are covered against a sandboxed home in
    tests/caller_cli_discovery_test.py. Here the question is the root-and-sudo
    shape, and this machine has every supported CLI installed host-wide -- that
    half of the classification would answer first and the case would prove
    nothing about the caller lookup. An unknown name classifies through exactly
    the same code (`agent_cli_binary` falls back to the name itself) and cannot
    be answered by whatever this host happens to have.
    """
    import shutil as _shutil

    from scripts import team_launcher as launcher

    for cli in ("syrd236-probe-cli", "hermes", "agy", "codex", "claude"):
        binary = launcher.agent_cli_binary(cli)
        if not _shutil.which(binary, path=launcher.DEFAULT_PANE_BASE_PATH):
            return cli, binary
    raise SystemExit("caller_cli_discovery_privileged_test: every candidate is host-wide here")


class Host:
    """Root, an operator account, and a PATH with nothing of theirs on it."""

    def __init__(self, account: pwd.struct_passwd, cli: str, binary: str) -> None:
        self.account = account
        self.cli = cli
        self.binary = binary
        self.home = Path(account.pw_dir)
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd236p."))
        self.tmp.chmod(0o755)
        bound = self.tmp / "home"
        bound.mkdir()
        subprocess.run(["mount", "--bind", str(bound), str(self.home)], check=True)
        os.chown(self.home, account.pw_uid, account.pw_gid)
        self.home.chmod(0o750)
        self.sanitized = self.tmp / "sanitized"
        self.sanitized.mkdir()

    def install(self, name: str = "", *, where: str = ".local/bin",
                owner_uid: int | None = None, mode: int = 0o755) -> Path:
        name = name or self.binary
        directory = self.home / where
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, self.account.pw_uid, self.account.pw_gid)
        directory.chmod(0o755)
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chown(path, self.account.pw_uid if owner_uid is None else owner_uid,
                 self.account.pw_gid)
        path.chmod(mode)
        return path

    def ask(self) -> dict:
        """What the shipped classification says, from a sudo-shaped process."""
        env = {
            "PATH": str(self.sanitized),
            "HOME": "/root",
            "SUDO_USER": self.account.pw_name,
            "SUDO_UID": str(self.account.pw_uid),
            "SYRD236_ROOT": str(ROOT),
            "SYRD236_CLI": self.cli,
        }
        result = subprocess.run(
            [sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def close(self) -> None:
        subprocess.run(["umount", str(self.home)], check=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


def the_operators_own_executable_is_found_from_root(host: Host) -> None:
    installed = host.install()
    said = host.ask()
    assert said["euid"] == 0, said
    assert said["host_wide_path"] == "", said
    # The account is the human sudo recorded, not this process.
    assert said["account_user"] == host.account.pw_name, said
    assert said["account_uid"] == host.account.pw_uid, said
    assert said["account_source"] == "sudo", said
    assert said["scope"] == "caller_only", said
    assert said["caller_path"] == str(installed), said
    assert said["caller_user"] == host.account.pw_name, said
    assert "is installed at" in said["explanation"], said
    assert host.account.pw_name in said["explanation"], said


def a_file_only_root_could_run_is_not_the_operators(host: Host) -> None:
    """Root's access() says yes to anything; the bits say whose it is."""
    host.install(owner_uid=0, mode=0o700)
    said = host.ask()
    assert said["scope"] == "absent", said
    assert said["caller_path"] == "", said
    # And the refusal says whose context was searched, and where.
    assert host.account.pw_name in said["explanation"], said
    assert str(host.home / ".local/bin") in said["explanation"], said


def a_home_root_cannot_attribute_is_simply_not_searched(host: Host) -> None:
    """Nothing installed for that account: absent, with the search named."""
    said = host.ask()
    assert said["scope"] == "absent", said
    assert said["account_home"] == str(host.home), said
    assert host.account.pw_name in said["explanation"], said


CASES = (
    the_operators_own_executable_is_found_from_root,
    a_file_only_root_could_run_is_not_the_operators,
    a_home_root_cannot_attribute_is_simply_not_searched,
)


def root_child() -> None:
    assert os.geteuid() == 0
    account = _candidate_account()
    cli, binary = _cli_this_host_does_not_have()
    for case in CASES:
        host = Host(account, cli, binary)
        try:
            case(host)
        finally:
            host.close()
        print(f"  ok   {case.__name__.replace('_', ' ')}")


def main() -> int:
    command = [sys.executable, str(Path(__file__).resolve()), "--root-child"]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", *command]
    else:
        command = ["unshare", "--mount", *command]
    subprocess.run(command, check=True)
    print(f"caller_cli_discovery_privileged_test: {len(CASES)} cases ok")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--root-child"]:
        root_child()
    else:
        raise SystemExit(main())
