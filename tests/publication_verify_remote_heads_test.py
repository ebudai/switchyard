#!/usr/bin/env python3
"""SYRD-747: `publication-status --verify` reads the remote's own HEAD, and runs as the owner from the owner's home.

MEFP, release 9e7253c5: root pinned the local bare repository `/data/git/fixpatch` as MEFP's publication remote, and
`switchyard publication-status mefp --verify`, run as root, reported unknown authority with
`fatal: error reading /root/.git`. Its check also took `git ls-remote <remote> HEAD | cut -f1` for the tip and
`git ls-remote --symref <remote> HEAD | awk '/^ref:/'` for the ref. `ls-remote` matches every ref ending in `/HEAD`,
and that valid repository holds `refs/heads/roles/director/HEAD` and `refs/remotes/origin/*/HEAD`, so both became
lists of lines and the dry-run push an invalid refspec.

Here the remote is a real bare repository of that shape, and the check is the real command, run as it is run: every
word after its `sudo -u <owner> -H`, the owner here being this account. It starts as root's session leaves it, with
`HOME=/root` and a working directory the owner cannot read, which is how git came to read `/root/.git`. The verdict is
`verify_shared_credential`'s, from that run.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


def mefp_shaped_remote(tmp: Path, *, head: str = "main") -> tuple[Path, str]:
    """A bare repository with MEFP's refs: HEAD on a branch, a branch named `roles/director/HEAD`, remote HEADs."""
    remote = tmp / "fixpatch.git"
    git("init", "-q", "--bare", str(remote))
    source = tmp / "source"
    git("init", "-q", str(source))
    git("commit", "-q", "--allow-empty", "-m", "one", cwd=source)
    first = git("rev-parse", "HEAD", cwd=source).strip()
    git("commit", "-q", "--allow-empty", "-m", "two", cwd=source)
    tip = git("rev-parse", "HEAD", cwd=source).strip()
    git("push", "-q", str(remote), f"{tip}:refs/heads/main", cwd=source)
    git("push", "-q", str(remote), f"{first}:refs/heads/roles/director/HEAD", cwd=source)
    git("push", "-q", str(remote), f"{first}:refs/remotes/origin/main", cwd=source)
    git("push", "-q", str(remote), f"{first}:refs/remotes/origin/app/main", cwd=source)
    git("--git-dir", str(remote), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git("--git-dir", str(remote), "symbolic-ref", "refs/remotes/origin/app/HEAD", "refs/remotes/origin/app/main")
    if head:
        git("--git-dir", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{head}")
    return remote, tip


def as_root_left_it(tmp: Path):
    """A runner that runs what follows the command's `sudo -u <owner> -H`, from root's session: HOME=/root, and a
    working directory the owner cannot read. Records the argv it was given."""
    locked = tmp / "root-home"
    locked.mkdir()
    scratch = tmp / "tmpdir"
    scratch.mkdir()
    seen: list[list[str]] = []

    def enter_unreadable_cwd() -> None:
        os.chdir(locked)
        os.chmod(locked, 0)

    def run(args, **kwargs):
        if args and args[0] == "install":  # the verdict's root-owned evidence file, written here as this account
            return subprocess.run([part for part in args if part not in ("-o", "root", "-g")], **kwargs)
        seen.append(list(args))
        assert args[:4] == ["sudo", "-u", pwd.getpwuid(os.getuid()).pw_name, "-H"], args
        try:
            return subprocess.run(args[4:], env={"PATH": os.environ["PATH"], "HOME": "/root", "TMPDIR": str(scratch)},
                                  preexec_fn=enter_unreadable_cwd, **kwargs)
        finally:
            os.chmod(locked, 0o700)

    return run, seen


def test_the_check_reads_only_the_remotes_own_head_and_runs_from_the_owners_home() -> None:
    from scripts.ticket_board import publication_boundary as pb

    me = pwd.getpwuid(os.getuid())
    with tempfile.TemporaryDirectory(prefix="syrd747.") as raw:
        tmp = Path(raw)
        remote, tip = mefp_shaped_remote(tmp)
        listed = git("ls-remote", str(remote), "HEAD").splitlines()
        check(len(listed) == 4, f"fixture: `ls-remote HEAD` matches the remote's HEAD and three others: {listed}")
        command = pb.shared_credential_check_command(str(remote), owner_user=me.pw_name, identity_file="/k")
        check(command[:7] == ["sudo", "-u", me.pw_name, "-H", "env", f"HOME={me.pw_dir}", "sh"],
              f"as the owner, with the owner's HOME from the account database, whoever runs it: {command[:7]}")
        check('cd "$HOME"' in command[-1], "and from it")
        os.environ["SWITCHYARD_PUBLISH_ROOT"] = str(tmp / "publish")
        (tmp / "publish").mkdir()
        runner, seen = as_root_left_it(tmp)
        finding = pb.verify_shared_credential("mefp", remote=str(remote), owner_user=me.pw_name, identity_file="/k",
                                              runner=runner)
        check(finding.state == pb.WRITE_PRESENT and finding.detail == "a dry-run push was accepted",
              f"run as root left it, the owner's dry-run push of the remote's own tip is answered: {finding}")
        check(list((tmp / "tmpdir").iterdir()) == [], "the empty repository it ran from is gone afterwards")
        script = seen[0][-1]
        # The refspec it pushed: the script, run again to print it instead of pushing.
        shown = subprocess.run(["sh", "-c", script.replace("git push --dry-run", "echo")],
                               env={"PATH": os.environ["PATH"], "HOME": me.pw_dir}, capture_output=True, text=True)
        check(shown.stdout.split()[-1] == f"{tip}:refs/heads/main",
              f"exactly the remote HEAD's tip onto the branch HEAD names, nothing else: {shown.stdout!r}")


def test_a_remote_with_no_branch_head_is_no_answer() -> None:
    from scripts.ticket_board import publication_boundary as pb

    me = pwd.getpwuid(os.getuid())
    with tempfile.TemporaryDirectory(prefix="syrd747.") as raw:
        tmp = Path(raw)
        remote, tip = mefp_shaped_remote(tmp)
        git("--git-dir", str(remote), "update-ref", "--no-deref", "HEAD", tip)  # HEAD detached: it names no branch
        os.environ["SWITCHYARD_PUBLISH_ROOT"] = str(tmp / "publish")
        (tmp / "publish").mkdir()
        runner, _seen = as_root_left_it(tmp)
        finding = pb.verify_shared_credential("mefp", remote=str(remote), owner_user=me.pw_name, identity_file="/k",
                                              runner=runner)
        check(finding.state == pb.WRITE_UNKNOWN,
              f"a HEAD that names no branch is no answer, not a verdict, and nothing is pushed: {finding}")
        missing = tmp / "nothing-here.git"
        finding = pb.verify_shared_credential("mefp", remote=str(missing), owner_user=me.pw_name, identity_file="/k",
                                              runner=runner)
        check(finding.state == pb.WRITE_UNKNOWN and "repository" in finding.detail,
              f"a remote that cannot be read is no answer, with git's own reason: {finding}")


def test_an_account_the_host_does_not_know_is_left_to_sudo() -> None:
    from scripts.ticket_board import publication_boundary as pb

    command = pb.shared_credential_check_command("/r.git", owner_user="syrd747-nobody", identity_file="/k")
    check(command[:5] == ["sudo", "-u", "syrd747-nobody", "-H", "sh"],
          f"no account to read a home from: sudo's -H sets it: {command[:5]}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"publication_verify_remote_heads_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
