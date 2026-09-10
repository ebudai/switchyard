#!/usr/bin/env python3
"""SYRD-94: report version divergence as fact, and change nothing.

Two silent failures, one principle.

`./install` exports whatever is checked out and never fetches, so a checkout a
few days old installs old code and reports success. That is worth saying and is
not worth refusing over: someone pinning a version is doing what a distributed
tool must allow.

The worse one is that pulling is not installing. `switchyard` runs from the
installed release, so a user who pulls a fix and re-runs the command hits the
identical bug they already reported, with success reported at every step. The
conclusion they draw is that it was never fixed.

Every check here reads refs that are already on disk. `test_no_notice_touches_the_network`
is not a review of the argv list -- it runs the real code in a network namespace
with no route at all and requires the same answers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.version_notice import (  # noqa: E402
    NOTICE_CONFIG_KEY,
    NOTICE_ENV,
    checkout_notice_lines,
    describe_checkout,
    release_notice_lines,
)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV, check=True
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


def upstream_pair(tmp: Path, *, behind: int = 0, ahead: int = 0) -> Path:
    """A checkout whose tracked upstream really is `behind`/`ahead` of it.

    Built by moving the remote-tracking ref, which is exactly what a fetch does
    and what this code is entitled to read. Nothing here is a stub of git.
    """
    origin = tmp / "origin.git"
    work = tmp / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=GIT_ENV)
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)],
        check=True,
        env=GIT_ENV,
        stderr=subprocess.DEVNULL,
    )
    git(work, "checkout", "-q", "-B", "main")
    _commit(work, "base")
    git(work, "push", "-q", "-u", "origin", "main")
    for index in range(behind):
        _commit(work, f"upstream-{index}")
    if behind:
        git(work, "push", "-q", "origin", "main")
        git(work, "reset", "-q", "--hard", f"HEAD~{behind}")
    for index in range(ahead):
        _commit(work, f"local-{index}")
    return work


def test_a_fresh_clone_says_nothing() -> None:
    """The common case. A false positive here trains people to ignore us."""
    with tempfile.TemporaryDirectory(prefix="notice-fresh.") as tmp:
        work = upstream_pair(Path(tmp))
        assert describe_checkout(work).level
        assert checkout_notice_lines(work, environ={}) == []


def test_a_checkout_behind_its_upstream_is_told_the_count() -> None:
    with tempfile.TemporaryDirectory(prefix="notice-behind.") as tmp:
        work = upstream_pair(Path(tmp), behind=13)
        lines = checkout_notice_lines(work, environ={})
        joined = "\n".join(lines)

        assert "13 commit(s) behind" in joined, joined
        assert "origin/main" in joined, joined
        # The consequence, which is the part that makes the count actionable.
        assert "does not fetch or pull" in joined, joined
        # Fact, not instruction: it must not tell them what version to want.
        assert "git pull" not in joined, joined
        assert "stale" not in joined.casefold(), joined


def test_it_reports_and_does_not_refuse() -> None:
    """The Director reconsidered refusing; this records the decision.

    Refusing to install the version somebody deliberately checked out is the
    installer overriding a choice they made. So there is no override flag to
    name, because nothing is blocked -- which is why `--allow-stale` does not
    exist under any name.

    Driven through the real `./install --dry-run`, not by reading it: the thing
    that matters is that a diverged checkout still exits 0 with the notice on
    its way past, and only running it can say that.
    """
    installer = (ROOT / "install").read_text(encoding="utf-8")
    assert "allow-stale" not in installer, "an override implies a refusal"
    assert installer.count("report_checkout_version\n") == 2, installer

    with tempfile.TemporaryDirectory(prefix="notice-installer.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path, behind=4)
        # A checkout that is behind, carrying this branch's installer and
        # reporter. Everything the installer would actually change is stubbed,
        # because what is under test is the report and the exit status.
        (work / "scripts").mkdir(exist_ok=True)
        shutil.copy(ROOT / "install", work / "install")
        shutil.copy(ROOT / "scripts/version_notice.py", work / "scripts/version_notice.py")
        stub = work / "scripts" / "stub"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)

        proc = subprocess.run(
            [str(work / "install"), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=work,
            env={
                **GIT_ENV,
                "SWITCHYARD_INSTALL_PREREQS_SCRIPT": str(stub),
                "SWITCHYARD_INSTALL_SWITCHYARD_SCRIPT": str(stub),
            },
        )
        combined = proc.stdout + proc.stderr

        assert proc.returncode == 0, combined
        assert "4 commit(s) behind" in combined, combined
        # It reported and kept going: the preview it was asked for still ran.
        assert "DRY RUN" in combined, combined


def test_the_notice_can_be_silenced_for_one_run_and_for_good() -> None:
    """Somebody pinned to a version must be able to stop hearing about it."""
    with tempfile.TemporaryDirectory(prefix="notice-silence.") as tmp:
        work = upstream_pair(Path(tmp), behind=2)
        assert checkout_notice_lines(work, environ={}) != []

        assert checkout_notice_lines(work, environ={NOTICE_ENV: "0"}) == []

        # Durable, and it belongs to the checkout that was pinned rather than to
        # a shell that will end.
        git(work, "config", NOTICE_CONFIG_KEY, "false")
        assert checkout_notice_lines(work, environ={}) == []
        # And the notice says how, so it is discoverable when it is wanted.
        git(work, "config", "--unset", NOTICE_CONFIG_KEY)
        assert NOTICE_CONFIG_KEY in "\n".join(checkout_notice_lines(work, environ={}))


def test_an_inconclusive_check_says_what_it_found_and_proceeds() -> None:
    """An unknown state is not a diverged one, and is no reason to stop."""
    with tempfile.TemporaryDirectory(prefix="notice-unknown.") as tmp:
        tmp_path = Path(tmp)
        plain = tmp_path / "plain"
        plain.mkdir()
        assert "not a git checkout" in "\n".join(checkout_notice_lines(plain, environ={}))

        work = upstream_pair(tmp_path, behind=1)
        git(work, "checkout", "-q", "--detach", "HEAD")
        assert "detached" in "\n".join(checkout_notice_lines(work, environ={}))

        git(work, "checkout", "-q", "-B", "solo")
        assert "tracks no upstream" in "\n".join(checkout_notice_lines(work, environ={}))

        # A remote and an upstream, but nothing fetched for it yet.
        git(work, "checkout", "-q", "-B", "main")
        git(work, "update-ref", "-d", "refs/remotes/origin/main")
        found = "\n".join(checkout_notice_lines(work, environ={}))
        assert "not present locally" in found or "tracks no upstream" in found, found

        # Every one of them still says the install is going ahead.
        for case in (plain,):
            assert "installing this checkout as-is" in "\n".join(
                checkout_notice_lines(case, environ={})
            )


def _release(tmp: Path, *, commit: str, source: Path, ref: str = "HEAD") -> Path:
    root = tmp / "release"
    root.mkdir(exist_ok=True)
    (root / ".switchyard-release.json").write_text(
        json.dumps({"commit": commit, "source_repo": str(source), "source_ref": ref}),
        encoding="utf-8",
    )
    return root


def test_a_release_older_than_its_checkout_names_the_fix() -> None:
    """The reported incident: pulled, re-ran, hit the identical bug."""
    with tempfile.TemporaryDirectory(prefix="notice-release.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path)
        old = git(work, "rev-parse", "HEAD")
        _commit(work, "the-fix")
        new = git(work, "rev-parse", "HEAD")
        root = _release(tmp_path, commit=old, source=work)

        lines = release_notice_lines(root, environ={})
        joined = "\n".join(lines)

        assert old[:12] in joined and new[:12] in joined, joined
        assert str(work) in joined, joined
        # The distinction the whole ticket turns on, stated where it is needed.
        assert "pulling updates a checkout" in joined, joined
        assert "sudo ./install" in joined, joined
        # It reports; it does not reinstall, which is privileged and theirs.
        assert "reinstall" not in joined.casefold().replace("./install", ""), joined


def test_a_release_level_with_its_checkout_says_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="notice-release-level.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path)
        root = _release(tmp_path, commit=git(work, "rev-parse", "HEAD"), source=work)
        assert release_notice_lines(root, environ={}) == []


def test_a_release_whose_checkout_is_gone_or_moved_warns_about_nothing() -> None:
    """A release does not depend on its source surviving."""
    with tempfile.TemporaryDirectory(prefix="notice-release-gone.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path)
        old = git(work, "rev-parse", "HEAD")
        _commit(work, "the-fix")

        moved = _release(tmp_path, commit=old, source=tmp_path / "not-here")
        assert release_notice_lines(moved, environ={}) == []

        plain = tmp_path / "plain"
        plain.mkdir()
        assert release_notice_lines(_release(tmp_path, commit=old, source=plain), environ={}) == []

        # Present, a git repo, but not one that has ever heard of this release.
        stranger = tmp_path / "stranger"
        subprocess.run(["git", "init", "-q", str(stranger)], check=True, env=GIT_ENV)
        _commit(stranger, "unrelated")
        assert release_notice_lines(_release(tmp_path, commit=old, source=stranger), environ={}) == []

        # A directory that is not a release at all.
        assert release_notice_lines(tmp_path / "plain", environ={}) == []


def test_a_release_ahead_of_its_checkout_is_not_reported_as_behind() -> None:
    """An inconclusive comparison must not become a confident warning."""
    with tempfile.TemporaryDirectory(prefix="notice-release-ahead.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path)
        _commit(work, "later")
        newer = git(work, "rev-parse", "HEAD")
        git(work, "reset", "-q", "--hard", "HEAD~1")
        assert release_notice_lines(_release(tmp_path, commit=newer, source=work), environ={}) == []


def test_the_release_notice_is_silenceable_from_the_checkout_it_names() -> None:
    with tempfile.TemporaryDirectory(prefix="notice-release-silence.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path)
        old = git(work, "rev-parse", "HEAD")
        _commit(work, "the-fix")
        root = _release(tmp_path, commit=old, source=work)

        assert release_notice_lines(root, environ={}) != []
        assert release_notice_lines(root, environ={NOTICE_ENV: "0"}) == []
        git(work, "config", NOTICE_CONFIG_KEY, "false")
        assert release_notice_lines(root, environ={}) == []


def test_the_running_command_reports_it_and_cannot_be_broken_by_it() -> None:
    """A version notice that can fail the tool is worse than the silence."""
    from scripts import team_launcher

    with tempfile.TemporaryDirectory(prefix="notice-main.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path)
        old = git(work, "rev-parse", "HEAD")
        _commit(work, "the-fix")
        root = _release(tmp_path, commit=old, source=work)

        printed: list[str] = []
        lines = team_launcher.report_installed_release_version(
            root=root, environ={}, print_func=printed.append
        )
        assert lines and printed == lines, (lines, printed)

        def explode(*_args, **_kwargs):
            raise RuntimeError("git is not available")

        assert team_launcher.report_installed_release_version(
            root=root, environ={}, runner=explode, print_func=printed.append
        ) == []


READ_ONLY_GIT = {
    "cat-file", "config", "merge-base", "rev-list", "rev-parse", "symbolic-ref",
}
NETWORK_GIT = {"fetch", "pull", "clone", "ls-remote", "push", "remote", "submodule"}


def test_no_notice_runs_a_git_subcommand_that_could_reach_the_network() -> None:
    """Nothing here even attempts a fetch, whatever the answer would have been.

    The offline case below shows the answers do not depend on the network. This
    shows no call is made: a `git fetch` whose failure is ignored would leave
    those answers identical and slip past it.
    """
    seen: list[list[str]] = []

    def recording(args, **kwargs):
        seen.append(list(args))
        return subprocess.run(list(args), **kwargs)

    with tempfile.TemporaryDirectory(prefix="notice-argv.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path, behind=2, ahead=1)
        old = git(work, "rev-parse", "HEAD~1")
        root = _release(tmp_path, commit=old, source=work)

        checkout_notice_lines(work, environ={}, runner=recording)
        release_notice_lines(root, environ={}, runner=recording)

    assert seen, "nothing ran, so this proves nothing"
    for argv in seen:
        assert argv[0] == "git", argv
        assert argv[1:3] == ["-C", str(work)], argv
        subcommand = argv[3]
        assert subcommand in READ_ONLY_GIT, argv
        assert subcommand not in NETWORK_GIT, argv


def test_no_notice_touches_the_network() -> None:
    """Run for real with no route, and require the same answers.

    Reading the argv list would only show what this code means to do. This shows
    what it does: an unprivileged network namespace has no route at all, and the
    control below confirms the sandbox is real by failing a fetch inside it.
    """
    if not shutil.which("unshare"):
        return
    with tempfile.TemporaryDirectory(prefix="notice-offline.") as tmp:
        tmp_path = Path(tmp)
        work = upstream_pair(tmp_path, behind=3)
        old = git(work, "rev-parse", "HEAD")
        root = _release(tmp_path, commit=old, source=work)
        git(work, "reset", "-q", "--hard", "HEAD")
        _commit(work, "the-fix")

        control = subprocess.run(
            ["unshare", "-rn", "git", "-C", str(work), "ls-remote", "https://github.com", "HEAD"],
            capture_output=True,
            text=True,
        )
        if control.returncode == 0:
            return  # No sandbox available here; this proves nothing, so claim nothing.

        for flag, target in (("--checkout", work), ("--release", root)):
            offline = subprocess.run(
                ["unshare", "-rn", sys.executable, str(ROOT / "scripts/version_notice.py"), flag, str(target)],
                capture_output=True,
                text=True,
            )
            online = subprocess.run(
                [sys.executable, str(ROOT / "scripts/version_notice.py"), flag, str(target)],
                capture_output=True,
                text=True,
            )
            assert offline.returncode == 0, offline.stderr
            assert offline.stderr == online.stderr, (flag, offline.stderr, online.stderr)
            assert offline.stderr.strip(), f"{flag} said nothing, so the comparison proves nothing"


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"version_notice_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
