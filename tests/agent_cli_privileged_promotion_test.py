#!/usr/bin/env python3
"""SYRD-211: accepting the promotion offer must work from an UNPRIVILEGED process.

The pre-bridge offer was placed correctly and then handed to
`promote_agent_cli_host_wide`, which stages a file under /usr/local/bin and
calls `chown(staged, 0, 0)` from inside the running process. Its only previous
caller was `switchyard new`, which already runs as root. `switchyard <project>`
does not, so accepting `[p]` could only ever fail:

    AgentCliUnavailable: switchyard: promoting claude needs root so the result
    is root-owned: [Errno 1] Operation not permitted ... euid=1006

Every positive case in the first suite injected a fake promoter, so none of them
ever ran the code an operator would actually reach. That is the second time on
this ticket that a stand-in hid the thing it stood in for, and it is why the
central case here runs the REAL default promoter, from a genuinely unprivileged
process, across an elevation boundary the KERNEL enforces -- a user namespace,
entered through a `sudo` on PATH, with a mount namespace putting a writable
directory where the real destination is.

Nothing about the privileged half is simulated: it is the shipped program, it
decides the destination itself, it does the atomic root-owned install, and it
drops privileges to run the promoted binary, which root must never execute.
"""

from __future__ import annotations

import json
import os
import shutil
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402

HELPER = ROOT / "scripts" / "switchyard-promote-agent-cli"
#: The uid the sandbox drops to when it stands in for the operator. It must be
#: mapped inside the namespace, which `--map-auto` arranges from /etc/subuid.
SANDBOX_OPERATOR_UID = 1000

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


def _namespaces_available() -> str:
    """Why this host cannot run the kernel-enforced case, or an empty string."""
    if shutil.which("unshare") is None:
        return "unshare is not installed"
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--map-auto", "--mount", "id", "-u"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "0":
        return f"user namespaces are unavailable: {(probe.stderr or '').strip()[:80]}"
    return ""


def _fake_cli(path: Path, banner: str) -> None:
    path.write_text(f'#!/bin/sh\necho "{banner}"\nexit 0\n', encoding="utf-8")
    path.chmod(0o755)


#: The tenant owner the sandbox verifies as. A real account on this host with a
#: uid inside the mapped range, distinct from the operator's -- which is the
#: whole point, since verifying as the operator proves only "not root".
#: /etc/passwd cannot be written inside the namespace (it belongs to the real
#: root), so these are looked up rather than invented.
SANDBOX_TENANT_UID = 1001
SANDBOX_TENANT_USER = pwd.getpwuid(SANDBOX_TENANT_UID).pw_name
SANDBOX_OPERATOR_USER = pwd.getpwuid(SANDBOX_OPERATOR_UID).pw_name


def _sandbox(tmp: Path) -> dict[str, Path]:
    """The root-owned locations the privileged half reads, as writable stand-ins."""
    layout = {
        "bin": tmp / "hostwide",
        "grants": tmp / "grants",
        "opt": tmp / "opt",
    }
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
    (layout["opt"] / "current" / "scripts").mkdir(parents=True, exist_ok=True)
    return layout


def _grant(layout: dict[str, Path], project: str, *, operator: str, owner: str) -> Path:
    directory = layout["grants"] / project
    directory.mkdir(parents=True, exist_ok=True)
    grant = directory / "control-grant.json"
    grant.write_text(
        json.dumps({"project": project, "owner": owner, "authorized_user": operator,
                    "launcher": "/opt/switchyard/current/switchyard"}),
        encoding="utf-8",
    )
    grant.chmod(0o644)
    return grant


def _recorder(layout: dict[str, Path], journal: Path) -> Path:
    """A stand-in for the rollout recorder that keeps what it was told."""
    recorder = layout["opt"] / "current" / "scripts" / "switchyard-record-rollout"
    recorder.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{journal}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    return recorder


def _sudo_shim(
    tmp: Path,
    layout: dict[str, Path],
    *,
    operator_uid: int = SANDBOX_OPERATOR_UID,
    prelude: str = "",
) -> Path:
    """A `sudo` that really becomes root -- by asking the kernel, not by lying.

    It enters a user namespace (so the caller is root there, which is the
    elevation) and a mount namespace, then binds writable stand-ins over the
    root-owned locations the privileged program reads and writes. The program is
    not told where any of them are; it still decides, and the kernel decides what
    that means.
    """
    shim = tmp / f"sudo-{operator_uid}"
    shim.write_text(
        "#!/bin/sh\n"
        "exec unshare --user --map-root-user --map-auto --mount /bin/sh -c '\n"
        f'  mount --bind "{layout["bin"]}" /usr/local/bin || exit 97\n'
        f'  mount --bind "{layout["grants"]}" /usr/local/lib/switchyard || exit 96\n'
        f'  mount --bind "{layout["opt"]}" /opt/switchyard || exit 95\n'
        f"  SUDO_UID={operator_uid} export SUDO_UID\n"
        f"  {prelude}\n"
        '  exec "$@"\n'
        "' _ \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def test_the_shipped_promoter_cannot_work_unprivileged_on_its_own() -> None:
    """The defect, reproduced against the function the offer used to call."""
    check(os.geteuid() != 0, "this case is only meaningful from a non-root caller")
    with tempfile.TemporaryDirectory(prefix="syrd211-eperm.") as tmp:
        root = Path(tmp)
        source = root / "claude"
        _fake_cli(source, "claude 1.2.3")
        destination = root / "bin"
        destination.mkdir()
        try:
            launcher.promote_agent_cli_host_wide(
                "claude", source, bin_dir=destination, print_func=lambda _l: None
            )
        except launcher.AgentCliUnavailable as exc:
            check("needs root so the result is root-owned" in str(exc),
                  f"it fails on the ownership boundary: {exc}")
        else:
            raise AssertionError(
                "an unprivileged process must not be able to produce a root-owned file"
            )


def test_a_helper_that_is_not_root_pinned_is_refused_by_the_production_default() -> None:
    """The pin, at the default an operator actually gets.

    The integration case below declares a sandbox stand-in for this one check,
    so this is the case that proves the real default is root and not the caller
    -- the mistake this ticket already made once.
    """
    check(os.geteuid() != 0, "this case is only meaningful from a non-root caller")
    try:
        launcher.promote_agent_cli_through_sudo(
            "claude", "/bin/sh", helper=HELPER, print_func=lambda _l: None,
            runner=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("sudo was reached with an unpinned helper")
            ),
        )
    except launcher.AgentCliUnavailable as exc:
        message = str(exc)
        check("refusing to run" in message, f"the crossing is refused: {message[:90]}")
        check("rather than by uid 0" in message,
              f"refused against ROOT, not against the caller: {message}")
    else:
        raise AssertionError("a checkout-owned helper must not be run as root")


def test_the_tenant_is_named_across_the_boundary() -> None:
    """Without it the privileged side has no grant to take an identity from.

    Dropping `--project` costs nothing visible on this side -- the promotion
    still succeeds -- and silently downgrades the far side to its no-tenant
    fallback, which is exactly the weaker check the second DAT rejection was
    about.
    """
    crossed: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="syrd211-project.") as tmp:
        source = Path(tmp) / "claude"
        _fake_cli(source, "claude 1.0")
        try:
            launcher.promote_agent_cli_through_sudo(
                "claude", source, project="test", helper=HELPER,
                helper_owner_uid=os.getuid(), helper_boundary=ROOT,
                runner=lambda args, **_k: (
                    crossed.append(list(args)) or subprocess.CompletedProcess(args, 0)
                ),
                which=lambda *_a, **_k: "/usr/local/bin/claude",
                print_func=lambda _l: None,
            )
        except launcher.AgentCliUnavailable as exc:  # pragma: no cover - would be a bug
            raise AssertionError(f"the crossing should have been attempted: {exc}") from exc
    check(crossed, "the boundary was crossed")
    argv = crossed[0]
    check("--project" in argv, f"the tenant is named: {argv}")
    check(argv[argv.index("--project") + 1] == "test",
          f"and it is the tenant being resumed: {argv}")
    check(argv.count("--project") == 1, f"named once: {argv}")


def test_a_nonzero_crossing_is_a_failure_not_a_promotion() -> None:
    """The privileged half said no; this side must not claim it said yes."""
    with tempfile.TemporaryDirectory(prefix="syrd211-exit.") as tmp:
        root = Path(tmp)
        source = root / "claude"
        _fake_cli(source, "claude 1.0")
        try:
            launcher.promote_agent_cli_through_sudo(
                "claude", source, helper=HELPER,
                helper_owner_uid=os.getuid(), helper_boundary=ROOT,
                runner=lambda args, **_k: subprocess.CompletedProcess(args, 3),
                which=lambda *_a, **_k: None,
                print_func=lambda _l: None,
            )
        except launcher.AgentCliUnavailable as exc:
            message = str(exc)
            check("exit 3" in message, f"the exit status is reported: {message}")
            check("any existing one is untouched" in message,
                  f"and the claim it makes is one the far side now keeps: {message}")
            check("nothing has been changed" not in message,
                  f"the old unconditional claim is gone: {message}")
        else:
            raise AssertionError("a refused crossing must not be reported as a promotion")


def test_an_unprivileged_operator_completes_a_real_promotion() -> None:
    """The case the first DAT rejection asked for. Nothing here is a mock."""
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped kernel-enforced promotion: {unavailable})")
        return
    check(os.geteuid() != 0, "the caller is unprivileged")
    with tempfile.TemporaryDirectory(prefix="syrd211-promote.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        layout = _sandbox(root)
        # setgid, so a file that merely inherited its directory's group would
        # carry that group instead of root's.
        os.chmod(layout["bin"], 0o2775)
        journal = root / "journal.txt"
        _recorder(layout, journal)
        source = root / "local" / "claude"
        source.parent.mkdir()
        _fake_cli(source, "claude 9.9.9")
        # setgid AND owned by another group, so a file that merely inherited its
        # directory would carry that group rather than root's.
        shim = _sudo_shim(root, layout, prelude="chgrp 1001 /usr/local/bin")

        said: list[str] = []
        verdict = launcher.promote_agent_cli_through_sudo(
            "claude", source,
            sudo_bin=str(shim), helper=HELPER,
            # A sandbox can neither own a file as root nor put one under a chain
            # of root-owned directories; the production default for both is
            # pinned by its own case above.
            helper_owner_uid=os.getuid(), helper_boundary=ROOT,
            which=lambda binary, path=None: (
                str(layout["bin"] / binary) if (layout["bin"] / binary).exists() else None
            ),
            print_func=said.append,
        )

        installed = layout["bin"] / "claude"
        check(installed.is_file(), f"the CLI was installed: {sorted(layout['bin'].iterdir())}")
        check(not installed.is_symlink(), "as a copy, never a link")
        check(installed.read_text() == source.read_text(), "with the source's bytes")
        info = installed.lstat()
        check(stat.S_IMODE(info.st_mode) == 0o755,
              f"mode 0755: {stat.S_IMODE(info.st_mode):04o}")
        check(info.st_uid == os.getuid(),
              f"owned by the namespace's root: uid {info.st_uid}")
        check(info.st_gid == os.getgid(),
              f"and group root, not inherited from the setgid directory: gid {info.st_gid}")
        check(_leftovers(layout["bin"]) == [],
              f"no staging or backup file was left: {_leftovers(layout['bin'])}")
        check(verdict.serves_a_new_owner, f"the verdict is host-wide: {verdict}")
        check(journal.is_file(), "the privileged mutation was journalled")
        entry = journal.read_text()
        check("agent-cli-promotion" in entry, f"under its own label: {entry}")
        check("--operator" in entry and SANDBOX_OPERATOR_USER in entry,
              f"naming the operator: {entry}")
        check("claude" in entry, f"and the CLI: {entry}")


def _leftovers(bin_dir: Path) -> list[str]:
    return sorted(
        entry.name for entry in bin_dir.iterdir()
        if entry.name.startswith(".") and "promote" in entry.name or ".previous." in entry.name
    )


def test_a_failing_candidate_never_replaces_a_working_one() -> None:
    """The first DAT rejection: verification used to run AFTER os.replace.

    A candidate whose `--version` fails destroyed the good host-wide copy and
    left the rejected bytes installed, while the caller reported that nothing
    had changed. The sentinel here is what makes that a test rather than a claim.
    """
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped destructive-replace case: {unavailable})")
        return
    with tempfile.TemporaryDirectory(prefix="syrd211-sentinel.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        layout = _sandbox(root)
        journal = root / "journal.txt"
        _recorder(layout, journal)

        sentinel = layout["bin"] / "claude"
        _fake_cli(sentinel, "claude GOOD 1.0")
        before = sentinel.read_bytes()
        before_mode = stat.S_IMODE(sentinel.lstat().st_mode)

        broken = root / "broken-claude"
        broken.write_text('#!/bin/sh\necho broken\nexit 7\n', encoding="utf-8")
        broken.chmod(0o755)

        result = subprocess.run(
            [str(_sudo_shim(root, layout)), str(HELPER), "claude", str(broken)],
            capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        check(result.returncode != 0, f"the promotion was refused: {output[:160]}")
        check("Nothing has been replaced" in output,
              f"and says so truthfully: {output[:200]}")
        check(sentinel.read_bytes() == before,
              "the working host-wide copy is byte-for-byte what it was")
        check(stat.S_IMODE(sentinel.lstat().st_mode) == before_mode,
              "with its mode intact")
        check(_leftovers(layout["bin"]) == [],
              f"and no staging or backup file remains: {_leftovers(layout['bin'])}")
        check(journal.is_file() and "failed" in journal.read_text(),
              f"the refusal is journalled: {journal.read_text() if journal.is_file() else '(none)'}")


def test_a_binary_that_only_works_for_the_operator_is_refused() -> None:
    """The second DAT rejection: verifying as the operator proves only 'not root'.

    The candidate here reads a mode-0600 file owned by the operator. Verified as
    the operator it passes; verified as the account that will actually run it,
    it cannot, and must be refused before anything is replaced.
    """
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped operator-only case: {unavailable})")
        return
    with tempfile.TemporaryDirectory(prefix="syrd211-operatoronly.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        layout = _sandbox(root)
        _recorder(layout, root / "journal.txt")
        _grant(layout, "test", operator=SANDBOX_OPERATOR_USER, owner=SANDBOX_TENANT_USER)

        sentinel = layout["bin"] / "claude"
        _fake_cli(sentinel, "claude GOOD 1.0")
        before = sentinel.read_bytes()

        secret = root / "operator-only"
        candidate = root / "needs-secret"
        candidate.write_text(
            "#!/bin/sh\n"
            f'cat "{secret}" >/dev/null 2>&1 || {{ echo cannot-read; exit 9; }}\n'
            'echo "claude 1.0"\nexit 0\n',
            encoding="utf-8",
        )
        candidate.chmod(0o755)

        prelude = (
            f'echo secret > "{secret}"; '
            f'chown {SANDBOX_OPERATOR_UID} "{secret}"; chmod 600 "{secret}"'
        )
        result = subprocess.run(
            [str(_sudo_shim(root, layout, prelude=prelude)),
             str(HELPER), "claude", str(candidate), "--project", "test"],
            capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        check(result.returncode != 0,
              f"a binary the tenant owner cannot run is refused: {output[:200]}")
        check("does not work for the tenant owner" in output,
              f"naming whose authority decided it: {output[:200]}")
        check(sentinel.read_bytes() == before,
              "and the working copy is untouched")
        check(_leftovers(layout["bin"]) == [],
              f"with nothing left staged: {_leftovers(layout['bin'])}")


def test_the_verification_identity_comes_from_the_root_owned_grant() -> None:
    """Not from the caller: the grant decides, and it must agree about who asks."""
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped grant corroboration: {unavailable})")
        return
    with tempfile.TemporaryDirectory(prefix="syrd211-grant.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        layout = _sandbox(root)
        _recorder(layout, root / "journal.txt")
        good = root / "claude"
        _fake_cli(good, "claude 1.0")

        def promote(project: str, *, operator_uid: int = SANDBOX_OPERATOR_UID, prelude: str = ""):
            return subprocess.run(
                [str(_sudo_shim(root, layout, operator_uid=operator_uid, prelude=prelude)),
                 str(HELPER), "claude", str(good), "--project", project],
                capture_output=True, text=True,
            )

        # A grant naming somebody else as the authorised operator.
        _grant(layout, "other", operator="somebody-else", owner=SANDBOX_TENANT_USER)
        wrong = promote("other")
        check(wrong.returncode != 0, "an operator the grant does not name is refused")
        check("is not the operator registered to control" in wrong.stdout + wrong.stderr,
              f"saying so: {(wrong.stdout + wrong.stderr)[:160]}")

        # A grant that is not root-owned is not tenant data.
        _grant(layout, "loose", operator=SANDBOX_OPERATOR_USER, owner=SANDBOX_TENANT_USER)
        (layout["grants"] / "loose" / "control-grant.json").chmod(0o666)
        loose = promote("loose")
        check(loose.returncode != 0, "a writable grant is refused")
        check("not root-owned and unwritable" in loose.stdout + loose.stderr,
              f"saying why: {(loose.stdout + loose.stderr)[:160]}")

        # No grant at all for a named project.
        missing = promote("absent")
        check(missing.returncode != 0, "a project with no grant is refused")

        check(not (layout["bin"] / "claude").exists(),
              "and after every refusal nothing was installed")


def test_the_promoted_cli_is_verified_as_the_tenant_and_never_as_root() -> None:
    """Root installs it; the account that will run it is the one that proves it."""
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped identity witness: {unavailable})")
        return
    with tempfile.TemporaryDirectory(prefix="syrd211-identity.") as tmp:
        root = Path(tmp)
        root.chmod(0o777)
        layout = _sandbox(root)
        os.chmod(layout["bin"], 0o777)
        _recorder(layout, root / "journal.txt")
        _grant(layout, "test", operator=SANDBOX_OPERATOR_USER, owner=SANDBOX_TENANT_USER)
        witness = root / "ran-as.txt"
        source = root / "claude"
        source.write_text(
            "#!/bin/sh\n"
            f'id -u > "{witness}"\n'
            'echo "claude 1.0"\nexit 0\n',
            encoding="utf-8",
        )
        source.chmod(0o755)
        result = subprocess.run(
            [str(_sudo_shim(root, layout)),
             str(HELPER), "claude", str(source), "--project", "test"],
            capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        check(result.returncode == 0, f"the promotion succeeded: {output[:250]}")
        check(witness.is_file(), f"the promoted binary was actually run: {output[:200]}")
        ran_as = witness.read_text().strip()
        check(ran_as != "0", f"and NOT as root -- it ran as uid {ran_as}")
        check(ran_as == str(SANDBOX_TENANT_UID),
              f"it ran as the tenant owner from the grant, not the operator: uid {ran_as}")
        check(f"verified as the tenant owner {SANDBOX_TENANT_USER}" in result.stdout,
              f"and says whose authority proved it: {result.stdout[:200]}")


def test_the_privileged_half_refuses_what_it_should() -> None:
    """The far side revalidates; it is not a root file-copy primitive."""
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped privileged refusals: {unavailable})")
        return
    with tempfile.TemporaryDirectory(prefix="syrd211-refuse.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        layout = _sandbox(root)
        _recorder(layout, root / "journal.txt")
        good = root / "claude"
        _fake_cli(good, "claude 1.0")

        def promote(cli: str, source: str, *, operator_uid: int = SANDBOX_OPERATOR_UID,
                    prelude: str = ""):
            return subprocess.run(
                [str(_sudo_shim(root, layout, operator_uid=operator_uid, prelude=prelude)),
                 str(HELPER), cli, source],
                capture_output=True, text=True,
            )

        refused = promote("definitely-not-a-cli", str(good))
        check(refused.returncode != 0, "an unknown CLI is refused")
        check("is not one of" in refused.stdout + refused.stderr,
              f"naming what is allowed: {(refused.stdout + refused.stderr)[:120]}")

        traversal = promote("../../etc/shadow", str(good))
        check(traversal.returncode != 0, "a path is not a CLI name")
        check("is not a CLI name" in traversal.stdout + traversal.stderr,
              f"refused before anything else: {(traversal.stdout + traversal.stderr)[:120]}")

        writable = root / "writable-claude"
        _fake_cli(writable, "claude 1.0")
        writable.chmod(0o777)
        loose = promote("claude", str(writable))
        check(loose.returncode != 0, "a world-writable source is refused")
        check("group- or world-writable" in loose.stdout + loose.stderr,
              f"saying why: {(loose.stdout + loose.stderr)[:140]}")

        directory = promote("claude", str(root))
        check(directory.returncode != 0, "a directory is not an executable")
        check("is not a regular file" in directory.stdout + directory.stderr,
              f"refused for being a directory, not incidentally while copying it: "
              f"{(directory.stdout + directory.stderr)[:140]}")

        plain = root / "plain"
        plain.write_text("not executable\n", encoding="utf-8")
        plain.chmod(0o644)
        not_exec = promote("claude", str(plain))
        check(not_exec.returncode != 0, "a non-executable source is refused")
        check("is not executable" in not_exec.stdout + not_exec.stderr,
              f"saying why: {(not_exec.stdout + not_exec.stderr)[:120]}")

        other = root / "someone-elses-claude"
        _fake_cli(other, "claude 1.0")
        foreign = promote("claude", str(other), prelude=f'chown 1002 "{other}"')
        check(foreign.returncode != 0, "a source owned by a third party is refused")
        check("neither root nor" in foreign.stdout + foreign.stderr,
              f"saying whose it is: {(foreign.stdout + foreign.stderr)[:140]}")

        system = promote("claude", str(good), operator_uid=0)
        check(system.returncode != 0, "a system uid may not promote")
        check("system account" in system.stdout + system.stderr,
              f"saying why: {(system.stdout + system.stderr)[:120]}")

        check(not (layout["bin"] / "claude").exists(),
              "and after every refusal nothing was installed")


def main() -> int:
    failures = 0
    for name, value in sorted(globals().items()):
        if not (name.startswith("test_") and callable(value)):
            continue
        try:
            value()
        except BaseException as exc:  # noqa: BLE001 - name what escaped
            failures += 1
            print(f"FAILED {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"agent_cli_privileged_promotion_test: {failures} failed")
        return 1
    print(f"agent_cli_privileged_promotion_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
