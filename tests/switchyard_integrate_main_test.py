#!/usr/bin/env python3
"""Security regressions for the Director-only integration operation.

After the credential cutover the shared account's key is read-only, so nothing a
role runs can push. This program is the one exception, for one ref, and it has
to be exactly as narrow as the publisher beside it: the live registered control
role, the exact tip the candidate was prepared against, forward only, one ref,
one pinned remote, a credential root alone can read.

Everything here runs unprivileged, with the root-owned locations redirected into
a temporary tree and the process ancestry supplied as a fake /proc. Git is real,
the remote is a real repository, and the pushes really happen.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import switchyard_publish_ref_test as publish_fixture

Host = publish_fixture.Host
PANE_PID = publish_fixture.PANE_PID
OTHER_PANE_PID = publish_fixture.OTHER_PANE_PID
SCRIPT = ROOT / "scripts" / "switchyard-integrate-main"

#: Stamp this process into the fake process table, then become the real program.
#: Passing 0 as the parent means "claim the parent I really have", so a chain of
#: these describes the ancestry that actually exists.
STAMP = (
    "import os, sys\n"
    "root, ppid = sys.argv[1], int(sys.argv[2])\n"
    "ppid = os.getppid() if ppid == 0 else ppid\n"
    "d = os.path.join(root, str(os.getpid()))\n"
    "os.makedirs(d, exist_ok=True)\n"
    "fields = ['S', str(ppid)] + ['0'] * 17 + ['7']\n"
    "open(os.path.join(d, 'stat'), 'w').write('%d (python3) ' % os.getpid() + ' '.join(fields) + '\\n')\n"
    "open(os.path.join(d, 'status'), 'w').write('Uid:\\t%d\\t%d\\t%d\\t%d\\n' % ((os.getuid(),) * 4))\n"
    "os.execv(sys.executable, [sys.executable] + sys.argv[3:])\n"
)


def load_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    loader = importlib.machinery.SourceFileLoader("switchyard_integrate_main", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


integrator = load_module()


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    if check:
        assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


class IntegrationHost(Host):
    """A publication host, plus an integration branch and a prepared merge."""

    def __init__(self, root: Path, *, integration_ref: str = "main") -> None:
        super().__init__(root)
        self.integration = integration_ref
        # The remote has an integration branch with history on it.
        git("commit", "--allow-empty", "-q", "-m", "trunk base", cwd=self.work)
        self.base = git("rev-parse", "HEAD", cwd=self.work)
        git("push", "-q", str(self.remote), f"HEAD:refs/heads/{integration_ref}", cwd=self.work)
        # And the control role prepares a descendant of exactly that tip.
        git("commit", "--allow-empty", "-q", "-m", "prepared integration merge", cwd=self.work)
        self.prepared = git("rev-parse", "HEAD", cwd=self.work)
        self.integration_bundle = root / "outbox" / "integration.bundle"
        git("branch", "-f", "candidate", self.prepared, cwd=self.work)
        git("bundle", "create", str(self.integration_bundle), "candidate", cwd=self.work)
        self.write_grant(integration_ref=integration_ref)

    def write_grant(self, **overrides) -> None:
        overrides.setdefault("integration_ref", getattr(self, "integration", "main"))
        super().write_grant(**overrides)

    def integrate(
        self,
        *,
        expected: str | None = None,
        commit: str | None = None,
        bundle: Path | None = None,
        under: int = PANE_PID,
        path: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        shim = (
            "import os, sys\n"
            "root, pane = sys.argv[1], int(sys.argv[2])\n"
            "d = os.path.join(root, str(os.getpid()))\n"
            "os.makedirs(d, exist_ok=True)\n"
            "fields = ['S', str(pane)] + ['0'] * 17 + ['7']\n"
            "open(os.path.join(d, 'stat'), 'w').write('%d (python3) ' % os.getpid() + ' '.join(fields) + '\\n')\n"
            "open(os.path.join(d, 'status'), 'w').write('Uid:\\t%d\\t%d\\t%d\\t%d\\n' % ((os.getuid(),) * 4))\n"
            "os.execv(sys.executable, [sys.executable] + sys.argv[3:])\n"
        )
        return subprocess.run(
            [
                "python3", "-c", shim, str(self.proc), str(under),
                str(SCRIPT),
                "--project", self.project,
                "--expected", expected if expected is not None else self.base,
                "--commit", commit if commit is not None else self.prepared,
                "--bundle", str(bundle if bundle is not None else self.integration_bundle),
            ],
            text=True,
            capture_output=True,
            env={
                **os.environ,
                **self.environment(),
                **({"PATH": path} if path else {}),
            },
        )

    def remote_integration(self) -> str:
        return git(
            "--git-dir", str(self.remote), "rev-parse", "--verify", "--quiet",
            f"refs/heads/{self.integration}",
            check=False,
        )


def refusal(result: subprocess.CompletedProcess[str], expected: str) -> None:
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert expected in combined, (expected, combined)


def test_the_registered_controller_fast_forwards_the_integration_branch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            result = host.integrate()
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout.strip().splitlines()[-1])
            assert report == {
                "integrated": True,
                "project": host.project,
                "ref": "main",
                "from": host.base,
                "commit": host.prepared,
                "control_role": "director",
                "cache": str(host.cache),
            }, report
            assert host.remote_integration() == host.prepared

            # The board verifies commits against the cache, so an integration
            # the cache does not know about is an integration nothing can build
            # on. Both names the tenant's tooling reads are moved.
            for name in ("refs/heads/main", "refs/remotes/origin/main"):
                assert git("--git-dir", str(host.cache), "rev-parse", f"{name}^{{commit}}") == host.prepared

            # Running it again is a no-op rather than an error: the tip is
            # already the commit, so there is nothing to move.
            repeat = host.integrate(expected=host.prepared)
            assert repeat.returncode == 0, repeat.stderr
            assert json.loads(repeat.stdout.strip().splitlines()[-1])["integrated"] is False
            assert host.remote_integration() == host.prepared
        finally:
            host.close()


def test_a_stale_lease_is_refused_and_the_branch_is_not_moved() -> None:
    """Somebody else integrated while this candidate was being prepared."""
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            # The branch moves under the caller's feet.
            git("commit", "--allow-empty", "-q", "-m", "somebody else's integration", cwd=host.work)
            other = git("rev-parse", "HEAD", cwd=host.work)
            git("push", "-q", str(host.remote), f"HEAD:refs/heads/{host.integration}", cwd=host.work)

            result = host.integrate(expected=host.base)
            refusal(result, "has moved")
            assert host.remote_integration() == other, "a refused integration must not have pushed"
        finally:
            host.close()


def test_only_a_descendant_of_the_stated_tip_can_be_integrated() -> None:
    """Forward only: no rewrite, no unrelated history, no side branch."""
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            # A commit that does not build on the current tip at all.
            unrelated = Path(raw) / "unrelated"
            git("init", "-q", "-b", "trunk", str(unrelated))
            git("config", "user.email", "other@example.invalid", cwd=unrelated)
            git("config", "user.name", "Other", cwd=unrelated)
            git("commit", "--allow-empty", "-q", "-m", "unrelated root", cwd=unrelated)
            stranger = git("rev-parse", "HEAD", cwd=unrelated)
            git("branch", "-f", "candidate", stranger, cwd=unrelated)
            stranger_bundle = Path(raw) / "outbox" / "stranger.bundle"
            git("bundle", "create", str(stranger_bundle), "candidate", cwd=unrelated)

            refusal(
                host.integrate(commit=stranger, bundle=stranger_bundle),
                "is not a descendant of",
            )
            assert host.remote_integration() == host.base

            # And a commit the bundle does not carry at all.
            refusal(host.integrate(commit="c" * 40), "does not contain")
            assert host.remote_integration() == host.base
        finally:
            host.close()


def test_a_branch_that_moves_mid_flight_is_refused_by_the_remote() -> None:
    """The window between reading the tip and pushing to it.

    Reading first is not enough: another integration can land in between, and
    only the remote can settle that. The lease travels with the push naming the
    exact OID this candidate was prepared against, so the server refuses instead
    of the last writer winning.

    The window is staged deterministically by moving the branch from inside the
    push itself -- a git on PATH that updates the remote and then becomes the
    real git -- which is the same thing a concurrent integration would do, at
    the only moment that matters.
    """
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            git("commit", "--allow-empty", "-q", "-m", "the other integration", cwd=host.work)
            intruder = git("rev-parse", "HEAD", cwd=host.work)
            # The other integration's objects really are on the remote, the way
            # a competing push would have put them there.
            git("push", "-q", str(host.remote), f"{intruder}:refs/heads/other", cwd=host.work)
            real_git = subprocess.run(["which", "git"], text=True, capture_output=True).stdout.strip()
            shim_dir = Path(raw) / "racing-bin"
            shim_dir.mkdir()
            (shim_dir / "git").write_text(
                "#!/bin/sh\n"
                "for arg in \"$@\"; do\n"
                "  if [ \"$arg\" = push ]; then\n"
                f"    {real_git} --git-dir {host.remote} update-ref refs/heads/{host.integration} {intruder} || exit 1\n"
                "    break\n"
                "  fi\n"
                "done\n"
                f"exec {real_git} \"$@\"\n",
                encoding="utf-8",
            )
            (shim_dir / "git").chmod(0o755)

            result = host.integrate(
                expected=host.base, path=f"{shim_dir}:{os.environ.get('PATH', '')}"
            )
            landed = host.remote_integration()
            assert result.returncode != 0, (
                "the branch moved between the read and the push and this integrated anyway; "
                f"the remote now reads {landed}"
            )
            combined = result.stdout + result.stderr
            assert "the remote refused the update" in combined, combined
            assert "stale info" in combined.lower(), combined
            # The commit that got there first is the one that stands.
            assert landed == intruder, (landed, intruder)
        finally:
            host.close()


def test_a_sibling_role_process_cannot_integrate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            refusal(
                host.integrate(under=OTHER_PANE_PID),
                "only director's registered process may publish",
            )
            assert host.remote_integration() == host.base
        finally:
            host.close()


def test_a_board_with_no_declared_controller_integrates_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            host.board.roles = [{"name": "director", "active": True, "capabilities": ["add_comment"]}]
            refusal(host.integrate(), "declares no role with control authority")
            assert host.remote_integration() == host.base
        finally:
            host.close()


def test_the_controller_is_derived_from_capabilities_here_too() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            host.board.roles = [
                {"name": "steward", "active": True,
                 "capabilities": ["merge", "set_manually_controlled", "resolve_publication"]},
                {"name": "director", "active": True, "capabilities": ["add_comment"]},
            ]
            host.board.assignments = {
                "steward": {
                    "role": "steward", "process_pid": PANE_PID,
                    "process_start_time": publish_fixture.PANE_START, "process_uid": host.uid,
                },
                "director": {
                    "role": "director", "process_pid": OTHER_PANE_PID,
                    "process_start_time": publish_fixture.OTHER_START, "process_uid": host.uid,
                },
            }
            refusal(host.integrate(under=OTHER_PANE_PID), "only steward's registered process")
            assert host.remote_integration() == host.base

            result = host.integrate()
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout.strip().splitlines()[-1])["control_role"] == "steward"
            assert host.remote_integration() == host.prepared
        finally:
            host.close()


def test_the_ref_and_the_remote_come_from_root_owned_data_only() -> None:
    """Nothing about where is caller-supplied: the flags do not exist."""
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            for flag in ("--ref", "--remote", "--repository", "--force"):
                rejected = subprocess.run(
                    [
                        "python3", str(SCRIPT), "--project", host.project,
                        "--expected", host.base, "--commit", host.prepared,
                        "--bundle", str(host.integration_bundle), flag, "value",
                    ],
                    text=True, capture_output=True, env={**os.environ, **host.environment()},
                )
                assert rejected.returncode != 0, (flag, rejected.stdout)
                assert "unrecognized arguments" in rejected.stderr, (flag, rejected.stderr)

            # A grant naming a role ref is refused: this program moves the
            # branch the project integrates into, and nothing else.
            host.write_grant(integration_ref="ops/topic")
            refusal(host.integrate(), "is not an integration branch")
            assert host.remote_integration() == host.base

            host.write_grant(remote="ext::sh -c 'touch /tmp/pwned'")
            refusal(host.integrate(), "would run a command rather than name a repository")
            host.write_grant(remote="")
            refusal(host.integrate(), "publish grant names no remote")
            assert host.remote_integration() == host.base
        finally:
            host.close()


def test_the_credential_and_the_bundle_are_held_to_the_same_rules() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            host.identity.chmod(0o640)
            refusal(host.integrate(), "a credential the project account can read")
            host.identity.chmod(0o600)

            link = Path(raw) / "outbox" / "link.bundle"
            link.symlink_to(host.integration_bundle)
            refusal(host.integrate(bundle=link), "must be a regular file, not a link")

            loose = Path(raw) / "outbox" / "loose.bundle"
            loose.write_bytes(host.integration_bundle.read_bytes())
            loose.chmod(0o666)
            refusal(host.integrate(bundle=loose), "writable by an account outside the project")

            not_a_bundle = Path(raw) / "outbox" / "empty.bundle"
            not_a_bundle.write_text("not a bundle\n", encoding="utf-8")
            refusal(host.integrate(bundle=not_a_bundle), "bundle did not verify")
            assert host.remote_integration() == host.base
        finally:
            host.close()


def test_a_malformed_commit_argument_never_reaches_git() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            for value in ("HEAD", "main", host.prepared[:12], "@{upstream}"):
                refusal(host.integrate(commit=value), "must be a full 40-character commit")
                refusal(host.integrate(expected=value), "must be a full 40-character commit")
            # An option-shaped value never becomes one: argparse takes it as a
            # flag and there is no flag by that name.
            refusal(host.integrate(commit="--all"), "expected one argument")
            assert host.remote_integration() == host.base
        finally:
            host.close()


def test_the_control_role_runs_one_command_and_the_branch_moves() -> None:
    """The whole Director half, with the driver a person actually types.

    It resolves the prepared commit, reads the tip itself, bundles the commit
    and hands all three to the privileged program -- which re-reads the tip from
    the remote it is pinned to and decides. Neither the driver nor its caller
    holds a credential.
    """
    with tempfile.TemporaryDirectory() as raw:
        host = IntegrationHost(Path(raw))
        try:
            root = Path(raw)
            (root / "stamp.py").write_text(STAMP, encoding="utf-8")
            wrapper = root / "integrator-shim"
            wrapper.write_text(
                "#!/bin/sh\n"
                f'exec python3 -c "$(cat {root / "stamp.py"})" {host.proc} 0 {SCRIPT} "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)

            driven = subprocess.run(
                [
                    "python3", "-c", STAMP, str(host.proc), str(PANE_PID),
                    str(ROOT / "scripts" / "switchyard-integrate"),
                    host.prepared,
                    "--project", host.project,
                    "--repository", str(host.work),
                    "--remote", str(host.remote),
                    "--ref", host.integration,
                    "--integrator", str(wrapper),
                    "--sudo", "",
                ],
                text=True,
                capture_output=True,
                env={**os.environ, **host.environment(), "HOME": str(host.owner_home)},
            )
            assert driven.returncode == 0, driven.stdout + driven.stderr
            assert host.remote_integration() == host.prepared, driven.stdout
            assert git("--git-dir", str(host.cache), "rev-parse", "refs/heads/main^{commit}") == host.prepared

            # And a candidate that is not a descendant is refused locally,
            # before anything privileged is reached at all.
            git("checkout", "-q", "-B", "sideways", host.base, cwd=host.work)
            git("commit", "--allow-empty", "-q", "-m", "a different line of work", cwd=host.work)
            sideways = git("rev-parse", "HEAD", cwd=host.work)
            refused = subprocess.run(
                [
                    "python3", str(ROOT / "scripts" / "switchyard-integrate"),
                    sideways,
                    "--project", host.project,
                    "--repository", str(host.work),
                    "--remote", str(host.remote),
                    "--ref", host.integration,
                    "--integrator", "/nonexistent/must-not-be-reached",
                    "--sudo", "",
                ],
                text=True,
                capture_output=True,
                env={**os.environ, **host.environment(), "HOME": str(host.owner_home)},
            )
            assert refused.returncode != 0
            assert "fast-forward only" in refused.stdout + refused.stderr, refused.stderr
            assert host.remote_integration() == host.prepared
        finally:
            host.close()


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"switchyard_integrate_main_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
