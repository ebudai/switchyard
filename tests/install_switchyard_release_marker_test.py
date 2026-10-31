#!/usr/bin/env python3
"""SYRD-710: install-switchyard sets release modes itself, whatever the umask, and repairs only markers it can trust.

SYRD-705's v7 cutover (2026-10-30): the root packet ran under umask 077, and
install-switchyard wrote `.switchyard-release.json` with a shell redirect, then
only removed group and other write bits. Every release it installed carried a
root-owned 0600 marker. PGU's tenant deploy migrated the database, then failed
reading `/opt/switchyard/releases/218a906d.../.switchyard-release.json` with
Permission denied, and was rolled back.

Everything here is real: the installer, this repository's own commit as its
source, and, for the tenant, the deploy's own marker reader
(`source_release_commit` from the installed release's `ticket-board-service.sh`)
and its Python one (`shared_release._read_switchyard_release_marker`). Root is
the root of a user namespace (`unshare --map-root-user --map-auto`), so the
installer runs as uid 0 and owns what it writes as root does. The tenant is a
mapped uid that reads a root file only through its "other" bits, as a tenant
account does. Nothing outside a test-owned directory is written. The suite
runs outside bwrap, like every namespace suite.
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
INSTALLER = ROOT / "scripts" / "install-switchyard"
TENANT = 1001
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def as_root(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """A shell as uid 0 of a user namespace, with root packet's umask."""
    return subprocess.run(["unshare", "--user", "--map-root-user", "--map-auto", "bash", "-c", f"umask 077\n{script}"],
                          capture_output=True, text=True, timeout=300, env={**os.environ, **(env or {})})


def as_tenant(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """A command as a non-root account that owns nothing the installer wrote."""
    return subprocess.run(["unshare", "--user", "--map-root-user", "--map-auto", "setpriv", f"--reuid={TENANT}",
                           f"--regid={TENANT}", "--clear-groups", *argv],
                          capture_output=True, text=True, timeout=120, env={**os.environ, **(env or {})})


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def main() -> int:
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="s710-"))
    tmp.chmod(0o755)
    shared = tmp / "opt" / "switchyard"
    env = {"SWITCHYARD_SOURCE_REPO": str(ROOT), "SWITCHYARD_SOURCE_REF": commit,
           "SWITCHYARD_SHARED_INSTALL_ROOT": str(shared), "SWITCHYARD_INSTALL_PATH": str(tmp / "bin" / "switchyard")}
    release = shared / "releases" / commit
    marker = release / ".switchyard-release.json"

    def install() -> subprocess.CompletedProcess[str]:
        return as_root(f"exec bash {INSTALLER} --apply", env)

    def tenant_reads() -> tuple[str, str]:
        """The tenant deploy's own marker check, and the Python reader release-status and staging use."""
        reader = (f"set -euo pipefail\nSOURCE_REPO={release}\nSWITCHYARD_RELEASE_MARKER_NAME=.switchyard-release.json\n"
                  f"eval \"$(sed -n '/^source_release_commit() {{/,/^}}/p' {release}/scripts/ticket-board-service.sh)\"\n"
                  "source_release_commit")
        deploy = as_tenant(["bash", "-c", reader])
        python = as_tenant([sys.executable, "-I", "-c",
                            f"import sys; sys.path.insert(0, {str(release)!r}); from pathlib import Path; "
                            "from scripts import shared_release; "
                            f"r = shared_release._read_switchyard_release_marker(Path({str(release)!r})); "
                            "print(r.marker_commit or r.marker_error)"])
        return (deploy.stdout.strip() if deploy.returncode == 0 else f"failed {deploy.returncode}: {deploy.stderr.strip()[-300:]}",
                python.stdout.strip() if python.returncode == 0 else f"failed: {python.stderr.strip()[-300:]}")

    try:
        check(as_root("id -u").stdout.strip() == "0", "the installer runs as uid 0 of its namespace")

        # 1. A fresh install under umask 077: modes are the installer's, not the umask's.
        done = install()
        check(done.returncode == 0, f"install-switchyard --apply succeeds under umask 077: {done.stderr[-1500:]}")
        check("repaired release marker" not in done.stderr, f"a fresh release is written right, not repaired: {done.stderr[-400:]}")
        check(mode(marker) == 0o644, f"the marker is 0644: {mode(marker):04o}")
        payload = json.loads(marker.read_text())
        check(payload["commit"] == commit, f"and names its release: {payload}")
        items = [path for path in release.rglob("*") if not path.is_symlink()]
        check(all(mode(path) & 0o022 == 0 for path in [release, *items]),
              f"no release content is writable by group or others: {[str(p) for p in items if mode(p) & 0o022][:5]}")
        check(all(mode(path) & 0o004 for path in items if path.is_file()),
              f"every release file is readable by others: {[str(p) for p in items if p.is_file() and not mode(p) & 0o004][:5]}")
        check(all(mode(path) == 0o755 for path in [release, *items] if path.is_dir()), "every release directory is 0755")
        check(mode(release / "switchyard") == 0o755 and mode(release / "scripts" / "team-launcher") == 0o755,
              "and its commands stay executable")
        check(os.readlink(shared / "current") == str(release), "current names the release")

        # 1b. As an account whose tar honours the umask (root's keeps the archive's modes), files are still readable.
        plain = tmp / "plain" / "switchyard"
        done = subprocess.run(["bash", "-c", f"umask 077; exec bash {INSTALLER} --apply"], capture_output=True, text=True,
                              timeout=300, env={**os.environ, **env, "SWITCHYARD_SHARED_INSTALL_ROOT": str(plain),
                                                "SWITCHYARD_INSTALL_PATH": str(tmp / "plain-bin" / "switchyard")})
        check(done.returncode == 0, f"a non-root install under umask 077 succeeds: {done.stderr[-800:]}")
        files = [path for path in (plain / "releases" / commit).rglob("*") if path.is_file() and not path.is_symlink()]
        check(files and all(mode(path) & 0o044 == 0o044 and mode(path) & 0o022 == 0 for path in files),
              f"and every file is readable, never writable, by group and others: "
              f"{[f'{p}:{mode(p):04o}' for p in files if mode(p) & 0o066 != 0o044][:3]}")

        # 2. The tenant reads and verifies it, through the deploy's own code and the Python reader.
        check(tenant_reads() == (commit, commit), f"a tenant's deploy and release readers read the marker: {tenant_reads()}")
        wrote = as_tenant(["sh", "-c", f"echo x > {release}/planted; echo y >> {marker}"])
        check(wrote.returncode != 0 and not (release / "planted").exists() and json.loads(marker.read_text()) == payload,
              f"and can write nothing in the release: {wrote.stderr.strip()}")

        # 3. Repair: markers an earlier install left 0600 -- this release's and an older one's -- are verified and widened.
        older = shared / "releases" / ("a" * 40)
        as_root(f"mkdir -m 0755 {older} && printf '%s' '{json.dumps({'commit': 'a' * 40})}' > {older}/.switchyard-release.json "
                f"&& chmod 0600 {marker} {older}/.switchyard-release.json")
        check(mode(marker) == 0o600 and tenant_reads()[0].startswith("failed 2"),
              f"a 0600 marker is what stopped the cutover: the tenant cannot read it: {tenant_reads()[0]}")
        done = install()
        check(done.returncode == 0 and f"repaired release marker {marker}: mode 0600 -> 0644" in done.stderr
              and f"repaired release marker {older}/.switchyard-release.json: mode 0600 -> 0644" in done.stderr,
              f"a reinstall repairs both: {done.stderr[-800:]}")
        check(mode(marker) == 0o644 and mode(older / ".switchyard-release.json") == 0o644, "both are 0644")
        check(tenant_reads() == (commit, commit), "and the tenant reads its release again")

        # 4. Refusals: nothing is trusted before it is checked, and a refusal changes nothing.
        decoy = tmp / "decoy.json"
        decoy.write_text(json.dumps({"commit": commit}))
        cases = [
            ("a symlink", f"rm {marker} && ln -s {decoy} {marker}", "cannot be opened without following a link"),
            ("another account's", f"chown {TENANT}:{TENANT} {marker}", f"belongs to uid {TENANT}, not 0"),
            ("for another commit", f"printf '%s' '{json.dumps({'commit': 'b' * 40})}' > {marker}", f"names commit '{'b' * 40}'"),
            ("not JSON", f"printf '%s' '{{' > {marker}", "is not readable JSON"),
            ("hard-linked", f"ln {marker} {tmp}/second-name", "has 2 links"),
            ("writable by others", f"chmod 0666 {marker}", "has mode 0666, which is more than 0644"),
            ("executable", f"chmod 0755 {marker}", "has mode 0755, which is more than 0644"),
            ("a FIFO", f"rm {marker} && mkfifo -m 0644 {marker}", "is not a regular file"),
            ("missing", f"rm {marker}", "is missing"),
            ("in a release another account owns", f"chown {TENANT} {release}", f"is uid {TENANT}, mode 0755"),
            ("under a releases directory another account owns", f"chown {TENANT} {shared}/releases", f"belongs to uid {TENANT}, not 0"),
            ("under a releases link", f"mv {shared}/releases {tmp}/real-releases && ln -s {tmp}/real-releases {shared}/releases",
             "cannot be opened as a directory without following a link"),
        ]
        good = marker.read_text()
        for label, plant, expected in cases:
            as_root(f"chmod 0600 {older}/.switchyard-release.json")
            planted = as_root(plant)
            check(planted.returncode == 0, f"planting {label}: {planted.stderr}")
            done = install()
            check(done.returncode != 0 and expected in done.stderr and "refusing to install; nothing was changed" in done.stderr,
                  f"a marker {label} refuses the install: {done.stderr[-600:]}")
            check(mode(older / ".switchyard-release.json") == 0o600,
                  f"and repairs nothing else first ({label})")
            check(os.readlink(shared / "current") == str(release), f"current is untouched ({label})")
            as_root(f"rm -f {tmp}/second-name; if [ -L {shared}/releases ]; then rm {shared}/releases && "
                    f"mv {tmp}/real-releases {shared}/releases; fi; chown 0:0 {shared}/releases {release}; "
                    f"rm -f {marker}; printf '%s' '{good}' > {marker}; chmod 0644 {marker}")
            check(install().returncode == 0, f"restored, it installs again ({label})")
    finally:
        as_root(f"rm -rf {tmp}")
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"install_switchyard_release_marker_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
