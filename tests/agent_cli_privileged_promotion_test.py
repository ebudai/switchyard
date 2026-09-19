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


def _sudo_shim(tmp: Path, bin_dir: Path, *, operator_uid: int = SANDBOX_OPERATOR_UID) -> Path:
    """A `sudo` that really becomes root -- by asking the kernel, not by lying.

    It enters a user namespace (so the caller is root there, which is the
    elevation) and a mount namespace, and binds the sandbox directory over the
    destination the privileged program is going to choose for itself. The
    program is not told where to write; it still decides, and the kernel decides
    what that means.
    """
    shim = tmp / "sudo"
    shim.write_text(
        "#!/bin/sh\n"
        "exec unshare --user --map-root-user --map-auto --mount /bin/sh -c '\n"
        f'  mount --bind "{bin_dir}" /usr/local/bin || exit 97\n'
        f"  SUDO_UID={operator_uid} export SUDO_UID\n"
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
            check("nothing has been changed" in message,
                  f"and nothing is claimed to have happened: {message}")
        else:
            raise AssertionError("a refused crossing must not be reported as a promotion")


def test_an_unprivileged_operator_completes_a_real_promotion() -> None:
    """The case the DAT rejection asked for. Nothing here is a mock.

    The promoter is the shipped default, this process is unprivileged, the
    elevation is a real user namespace, and the program that installs the file
    is the real `switchyard-promote-agent-cli` deciding its own destination.
    """
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped kernel-enforced promotion: {unavailable})")
        return
    check(os.geteuid() != 0, "the caller is unprivileged")
    with tempfile.TemporaryDirectory(prefix="syrd211-promote.") as tmp:
        root = Path(tmp)
        bin_dir = root / "hostwide"
        bin_dir.mkdir()
        # setgid, owned by another group: a real shape for a shared
        # /usr/local/bin, and the reason the install sets ownership explicitly
        # instead of trusting what the directory hands a new file.
        os.chmod(bin_dir, 0o2775)
        source = root / "local" / "claude"
        source.parent.mkdir()
        _fake_cli(source, "claude 9.9.9")
        # Give the bind source a group the promoted file must NOT inherit.
        subprocess.run([str(_sudo_shim(root, bin_dir)), "/bin/sh", "-c",
                        f'chgrp 1001 "{bin_dir}" 2>/dev/null; true'],
                       capture_output=True, text=True)
        shim = _sudo_shim(root, bin_dir)

        said: list[str] = []
        # The real default promoter. Only the sudo binary and the pin stand-in
        # are supplied; the crossing, the privileged program and the install are
        # the shipped ones.
        verdict = launcher.promote_agent_cli_through_sudo(
            "claude", source,
            sudo_bin=str(shim), helper=HELPER,
            # A sandbox can neither own a file as root nor put one under a
            # chain of root-owned directories; the production default for both
            # is pinned by its own case above.
            helper_owner_uid=os.getuid(), helper_boundary=ROOT,
            which=lambda binary, path=None: (
                str(bin_dir / binary) if (bin_dir / binary).exists() else None
            ),
            print_func=said.append,
        )

        installed = bin_dir / "claude"
        check(installed.is_file(), f"the CLI was installed: {sorted(bin_dir.iterdir())}")
        check(not installed.is_symlink(), "as a copy, never a link")
        check(installed.read_text() == source.read_text(), "with the source's bytes")
        info = installed.lstat()
        check(stat.S_IMODE(info.st_mode) == 0o755,
              f"mode 0755: {stat.S_IMODE(info.st_mode):04o}")
        # Inner uid 0 is this uid outside the namespace, so root-owned in there
        # is exactly this ownership out here.
        check(info.st_uid == os.getuid(),
              f"owned by the namespace's root: uid {info.st_uid}")
        # The bind source is setgid to a non-root group, so a file that merely
        # inherited its directory's group would carry that group instead.
        check(info.st_gid == os.getgid(),
              f"and group root, not inherited from the setgid directory: gid {info.st_gid}")
        leftovers = [entry.name for entry in bin_dir.iterdir()
                     if entry.name.startswith(".claude.promote.")]
        check(leftovers == [], f"and no staging file was left behind: {leftovers}")
        check(verdict.serves_a_new_owner, f"the verdict is host-wide: {verdict}")
        text = "\n".join(said)
        check("only the CLI name and that path cross" in text,
              f"the operator is told what crosses: {text}")


def test_the_privileged_half_refuses_what_it_should() -> None:
    """The far side revalidates; it is not a root file-copy primitive."""
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped privileged refusals: {unavailable})")
        return
    with tempfile.TemporaryDirectory(prefix="syrd211-refuse.") as tmp:
        root = Path(tmp)
        bin_dir = root / "hostwide"
        bin_dir.mkdir()
        good = root / "claude"
        _fake_cli(good, "claude 1.0")
        shim = _sudo_shim(root, bin_dir)

        def promote(cli: str, source: str, *, operator_uid: int = SANDBOX_OPERATOR_UID):
            return subprocess.run(
                [str(_sudo_shim(root, bin_dir, operator_uid=operator_uid)),
                 str(HELPER), cli, source],
                capture_output=True, text=True,
            )

        # A CLI name that is not on the privileged side's own list.
        refused = promote("definitely-not-a-cli", str(good))
        check(refused.returncode != 0, "an unknown CLI is refused")
        check("is not one of" in refused.stdout + refused.stderr,
              f"naming what is allowed: {(refused.stdout + refused.stderr)[:120]}")

        # A path that tries to name the destination instead of a CLI.
        traversal = promote("../../etc/shadow", str(good))
        check(traversal.returncode != 0, "a path is not a CLI name")
        check("is not a CLI name" in traversal.stdout + traversal.stderr,
              f"refused before anything else: {(traversal.stdout + traversal.stderr)[:120]}")

        # A source anyone could rewrite between the check and the copy.
        writable = root / "writable-claude"
        _fake_cli(writable, "claude 1.0")
        writable.chmod(0o777)
        loose = promote("claude", str(writable))
        check(loose.returncode != 0, "a world-writable source is refused")
        check("group- or world-writable" in loose.stdout + loose.stderr,
              f"saying why: {(loose.stdout + loose.stderr)[:140]}")

        # A directory, and a non-executable file.
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

        # A source belonging to neither root nor the operator. The mount
        # namespace maps a range, so a second uid really exists to own it.
        other = root / "someone-elses-claude"
        _fake_cli(other, "claude 1.0")
        foreign = subprocess.run(
            [str(shim), "/bin/sh", "-c",
             f'chown 1001 "{other}" && exec "$@"', "_",
             str(HELPER), "claude", str(other)],
            capture_output=True, text=True,
        )
        check(foreign.returncode != 0, "a source owned by a third party is refused")
        check("neither root nor" in foreign.stdout + foreign.stderr,
              f"saying whose it is: {(foreign.stdout + foreign.stderr)[:140]}")

        # A system account may not drive this.
        system = promote("claude", str(good), operator_uid=0)
        check(system.returncode != 0, "a system uid may not promote")
        check("system account" in system.stdout + system.stderr,
              f"saying why: {(system.stdout + system.stderr)[:120]}")

        check(not (bin_dir / "claude").exists(),
              "and after every refusal nothing was installed")


def test_the_promoted_cli_is_never_executed_as_root() -> None:
    """Root installs it; root must not run it.

    The promoted binary reports the uid that ran it, so this is read from the
    program's own behaviour rather than from the source of the helper.
    """
    unavailable = _namespaces_available()
    if unavailable:
        print(f"  (skipped root-execution check: {unavailable})")
        return
    with tempfile.TemporaryDirectory(prefix="syrd211-asroot.") as tmp:
        root = Path(tmp)
        # The verification deliberately drops to the operator, so the witness
        # has to be writable by that uid -- otherwise the redirect fails and the
        # missing file reads like "it was never run".
        root.chmod(0o777)
        bin_dir = root / "hostwide"
        bin_dir.mkdir()
        witness = root / "ran-as.txt"
        source = root / "claude"
        source.write_text(
            "#!/bin/sh\n"
            f'id -u > "{witness}"\n'
            'echo "claude 1.0"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        source.chmod(0o755)
        shim = _sudo_shim(root, bin_dir)
        result = subprocess.run(
            [str(shim), str(HELPER), "claude", str(source)],
            capture_output=True, text=True,
        )
        check(result.returncode == 0,
              f"the promotion succeeded: {(result.stdout + result.stderr)[:200]}")
        check(witness.is_file(), "the promoted binary was actually run to verify it")
        ran_as = witness.read_text().strip()
        check(ran_as != "0",
              f"and NOT as root -- it ran as uid {ran_as}")
        check(ran_as == str(SANDBOX_OPERATOR_UID),
              f"it ran as the operator: uid {ran_as}")
        check("runs as" in result.stdout, f"and says so: {result.stdout[:160]}")


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
