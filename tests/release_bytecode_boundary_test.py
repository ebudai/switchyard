#!/usr/bin/env python3
"""SYRD-711: root-side imports leave no bytecode in a shared release, and the installer removes what earlier ones left.

The SYRD-698 fleet proof found root-owned `__pycache__` directories inside 74 of
the host's `/opt/switchyard/releases/<commit>` trees: `switchyard-record-rollout`
and other programs imported release modules as root without `-B`, and SYRD-705's
trust checks did the same under `umask 077`, leaving 0700 caches. A release is no
longer its commit's archive, and a later tree comparison cannot tell the two apart.

1. Every Python program in this tree that loads tree code sets
   `sys.dont_write_bytecode` before its first such load: a static check of each
   program, so a new one cannot regress it.
2. The repository tracks no bytecode.
3. As root (the root of a user namespace, as in the SYRD-710 suite), an install
   under `umask 022` and under `umask 077`, then the root-side programs run from
   the release again and again, leave the release exactly its commit's archive
   plus its marker.
4. Caches an earlier root import left -- this release's 0700 ones, an older
   release's 0755 ones and a stray `.pyc` -- are removed by the next install,
   each named; the release is archive-exact again; `current` and the markers are
   as they were, and no `systemctl` is run.
5. Anything else refuses the install before any file is changed: a linked cache
   or `.pyc`, another account's cache or file, a hard link, a cache or an
   ancestor directory writable by others or not root's, a foreign file or a
   directory inside a cache, a FIFO named `.pyc`. An older release's safe cache
   and narrow marker are left as they were.
"""

from __future__ import annotations

import ast
import hashlib
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


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def as_root(script: str, env: dict[str, str] | None = None, umask: str = "077") -> subprocess.CompletedProcess[str]:
    """A shell as uid 0 of a user namespace, at a root packet's umask unless told otherwise."""
    return subprocess.run(["unshare", "--user", "--map-root-user", "--map-auto", "bash", "-c", f"umask {umask}\n{script}"],
                          capture_output=True, text=True, timeout=600, env={**os.environ, **(env or {})})


# -- 1. the programs ----------------------------------------------------------------------------------------------


def programs() -> list[str]:
    """Every Python program here: a Python shebang, and either an executable mode or a `__main__` block."""
    found = []
    for line in git("ls-files", "-s", "scripts", "switchyard").splitlines():
        mode, _sha, _stage_path = line.split(" ", 2)
        path = _stage_path.split("\t", 1)[1]
        file = ROOT / path
        if mode == "120000" or not file.is_file():
            continue
        try:
            source = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        first = source.splitlines()[0] if source else ""
        if first.startswith("#!") and "python" in first and (
                mode == "100755" or "__name__ == \"__main__\"" in source or "__name__ == '__main__'" in source):
            found.append(path)
    return found


def tree_modules() -> set[str]:
    """What a program here can import from the tree: the packages, and every module beside one on its path."""
    return {"scripts", "ticket_board"} | {Path(f).stem for f in git("ls-files").split()
                                          if f.endswith(".py") and Path(f).parent.name in ("scripts", "ticket_board", "tests")}


def loads_tree(statement: ast.stmt, modules: set[str]) -> bool:
    for node in ast.walk(statement):
        if isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").split(".")[0] in modules):
            return True
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] in modules for alias in node.names):
            return True
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in ("spec_from_file_location", "import_module", "__import__"):
                return True
    return False


def suppresses(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant) and statement.value.value is True \
        and any(isinstance(t, ast.Attribute) and t.attr == "dont_write_bytecode" and isinstance(t.value, ast.Name)
                and t.value.id == "sys" for t in statement.targets)


def check_programs() -> None:
    modules, loading = tree_modules(), []
    for path in programs():
        body = ast.parse((ROOT / path).read_text(encoding="utf-8")).body
        first = next((i for i, statement in enumerate(body) if loads_tree(statement, modules)), None)
        if first is None:
            continue
        loading.append(path)
        check(any(suppresses(statement) for statement in body[:first]),
              f"{path} sets sys.dont_write_bytecode = True before it first loads tree code (line {body[first].lineno})")
    for path in ("scripts/switchyard-record-rollout", "scripts/owner_runtime_dirs.py", "scripts/switchyard-install-authority",
                 "scripts/ticket-board-provision-project", "switchyard"):
        check(path in loading, f"{path}, a root-side program, is among those checked")
    for path in ("scripts/tenant_paths.py", "scripts/ticket_board/migration_order.py"):
        # Root runs these as `/usr/bin/python3 -I <release>/...` and no flag: they cache nothing only while they
        # import the standard library alone.
        body = ast.parse((ROOT / path).read_text(encoding="utf-8")).body
        check(not any(loads_tree(statement, modules) for statement in body), f"{path} loads no tree code")


# -- 3 to 5. the installer ------------------------------------------------------------------------------------------


def archive(commit: str, into: Path) -> dict[str, str]:
    """The commit's archive: relative path -> type and content digest."""
    into.mkdir(parents=True)
    subprocess.run(f"git -C {ROOT} archive {commit} | tar -C {into} -x", shell=True, check=True)
    return listing(into)


def listing(top: Path) -> dict[str, str]:
    """Relative path -> what it is: a directory, a link and its target, or a file's sha256."""
    out = {}
    for path in sorted(top.rglob("*")):
        rel = str(path.relative_to(top))
        if path.is_symlink():
            out[rel] = f"link {os.readlink(path)}"
        elif path.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def main() -> int:
    check_programs()
    tracked = [f for f in git("ls-files").split() if "__pycache__" in f.split("/") or f.endswith(".pyc")]
    check(not tracked, f"the repository tracks no bytecode: {tracked[:5]}")

    commit = git("rev-parse", "HEAD").strip()
    tmp = Path(tempfile.mkdtemp(prefix="s711-"))
    tmp.chmod(0o755)
    shared = tmp / "opt" / "switchyard"
    shim = tmp / "shim"
    shim.mkdir()
    called = tmp / "systemctl-called"
    (shim / "systemctl").write_text(f"#!/bin/sh\necho \"$@\" >> {called}\nexit 1\n")
    (shim / "systemctl").chmod(0o755)
    env = {"SWITCHYARD_SOURCE_REPO": str(ROOT), "SWITCHYARD_SOURCE_REF": commit,
           "SWITCHYARD_SHARED_INSTALL_ROOT": str(shared), "SWITCHYARD_INSTALL_PATH": str(tmp / "bin" / "switchyard"),
           "PATH": f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    release = shared / "releases" / commit
    expected = archive(commit, tmp / "archive")
    marker = ".switchyard-release.json"

    def install(umask: str = "077") -> subprocess.CompletedProcess[str]:
        return as_root(f"exec bash {INSTALLER} --apply", env, umask)

    def exact(where: Path = release) -> list[str]:
        """How the release differs from its commit's archive plus its marker."""
        have = listing(where)
        return sorted(set(have) ^ ({*expected, marker}) | {p for p in expected if have.get(p, expected[p]) != expected[p]})

    def root_programs() -> subprocess.CompletedProcess[str]:
        """The root-side programs, from the release, as root under the restrictive umask, each twice."""
        lines = [f"{release}/switchyard --version", f"{release}/scripts/switchyard-record-rollout --help",
                 f"{release}/scripts/ticket-board-read --help", f"{release}/scripts/switchyard-install-authority --help",
                 f"/usr/bin/python3 -I {release}/scripts/owner_runtime_dirs.py --help",
                 f"/usr/bin/python3 -I {release}/scripts/tenant_paths.py --help",
                 f"/usr/bin/python3 -I -B -c 'import sys; sys.path.insert(0, \"{release}\"); "
                 "from scripts.ticket_board import privileged_operations'"]
        script = "set -e\ncd /\n" + "\n".join(f"{line} >/dev/null\n{line} >/dev/null" for line in lines)
        return as_root(script, env)

    def caches_import() -> subprocess.CompletedProcess[str]:
        """The import that left the live 0700 caches: release modules as root, no -B, under umask 077."""
        return as_root(f"cd / && /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, \"{release}\"); "
                       "import scripts.ticket_board.rollout_journal, scripts.tenant_paths'", env)

    try:
        check(as_root("id -u").stdout.strip() == "0", "the installer runs as uid 0 of its namespace")

        # 3. Installs under both umasks, then the root-side programs, leave the release exactly its archive.
        for umask in ("022", "077"):
            as_root(f"rm -rf {shared}")
            done = install(umask)
            check(done.returncode == 0, f"install-switchyard --apply under umask {umask}: {done.stderr[-1200:]}")
            check("removed bytecode cache" not in done.stderr, f"a fresh install has no cache to remove (umask {umask})")
            check(not exact(), f"the installed release is its archive plus its marker (umask {umask}): {exact()[:5]}")
            ran = root_programs()
            check(ran.returncode == 0, f"the root-side programs run from the release (umask {umask}): {ran.stderr[-800:]}")
            check(not exact(), f"and, run twice each as root, leave it exact (umask {umask}): {exact()[:5]}")
        check(not called.exists(), "no install ran systemctl")

        # 4. What earlier root imports left is removed by the next install, and nothing else changes.
        older = shared / "releases" / ("a" * 40)
        planted = as_root(
            f"mkdir -p {older}/scripts/ticket_board/__pycache__ && printf '%s' '{json.dumps({'commit': 'a' * 40})}' "
            f"> {older}/{marker} && chmod 0600 {older}/{marker} && chmod -R 0755 {older} && chmod 0600 {older}/{marker} "
            f"&& printf x > {older}/scripts/ticket_board/__pycache__/__init__.cpython-314.pyc "
            f"&& printf x > {older}/scripts/stray.pyc")
        check(planted.returncode == 0, f"planting an older release's caches: {planted.stderr}")
        made = caches_import()
        check(made.returncode == 0 and (release / "scripts" / "ticket_board" / "__pycache__").is_dir()
              and (release / "scripts" / "__pycache__").is_dir(),
              f"the import that left the live caches leaves them here too: {made.stderr[-400:]}")
        check(mode(release / "scripts" / "ticket_board" / "__pycache__") == 0o700, "as root-only 0700 directories")
        before = os.readlink(shared / "current")
        done = install()
        check(done.returncode == 0, f"the next install succeeds: {done.stderr[-1200:]}")
        for gone in (release / "scripts" / "ticket_board" / "__pycache__", release / "scripts" / "__pycache__",
                     older / "scripts" / "ticket_board" / "__pycache__", older / "scripts" / "stray.pyc"):
            check(f"removed bytecode cache {gone}" in done.stderr and not os.path.lexists(gone),
                  f"and removes {gone}, naming it: {done.stderr[-800:]}")
        check(not exact(), f"the release is its archive again: {exact()[:5]}")
        check(os.readlink(shared / "current") == before and (release / marker).exists(), "current and the marker stand")
        check(mode(older / marker) == 0o644, "the older release's narrow marker is repaired as before (SYRD-710)")
        check(not called.exists(), "and no board was touched: no systemctl")

        # 5. Anything the installer cannot prove its own refuses the install, before any file is changed.
        cache = release / "scripts" / "ticket_board" / "__pycache__"
        pyc = cache / "rollout_journal.cpython-314.pyc"
        elsewhere = tmp / "elsewhere"
        elsewhere.mkdir()
        cases = [
            ("a linked cache", f"rm -rf {cache} && ln -s {elsewhere} {cache}", f"{cache} is a symbolic link"),
            ("a linked .pyc", f"rm {pyc} && ln -s {elsewhere}/x.pyc {pyc}", f"{pyc} is not a regular file"),
            ("another account's .pyc", f"chown {TENANT} {pyc}", f"{pyc} belongs to uid {TENANT}, not 0"),
            ("another account's cache", f"chown {TENANT} {cache}", f"{cache} is uid {TENANT}, mode 0700"),
            ("a hard-linked .pyc", f"ln {pyc} {tmp}/second-name", f"{pyc} has 2 links"),
            ("a cache writable by others", f"chmod 0777 {cache}", f"{cache} is uid 0, mode 0777"),
            ("a parent writable by others", f"chmod 0777 {cache.parent}", f"is under {cache.parent}, which is uid 0, mode 0777"),
            ("a parent another account owns", f"chown {TENANT} {cache.parent}",
             f"is under {cache.parent}, which is uid {TENANT}, mode 0755"),
            # Audit, round 1: a safe parent under an unsafe grandparent, or under an unsafe release, is no safer. The
            # grandparent's own cache goes first, so only the grandparent stands above the cache judged.
            ("a grandparent writable by others", f"rm -rf {cache.parent.parent}/__pycache__ && chmod 0777 {cache.parent.parent}",
             f"is under {cache.parent.parent}, which is uid 0, mode 0777"),
            ("a grandparent another account owns",
             f"rm -rf {cache.parent.parent}/__pycache__ && chown {TENANT} {cache.parent.parent}",
             f"is under {cache.parent.parent}, which is uid {TENANT}, mode 0755"),
            ("the release itself writable by others", f"chmod 0777 {release}", f"is under {release}, which is uid 0, mode 0777"),
            ("a foreign file in a cache", f"printf x > {cache}/notes.txt", f"{cache}/notes.txt is not a .pyc file"),
            ("a directory in a cache", f"mkdir {cache}/inner", f"{cache}/inner is not a regular file"),
            ("a FIFO named .pyc", f"mkfifo {release}/scripts/odd.pyc", f"{release}/scripts/odd.pyc is neither"),
        ]
        for label, plant, message in cases:
            reset = as_root(f"rm -f {tmp}/second-name; chmod 0755 {release} {cache.parent.parent} {cache.parent}; "
                            f"chown 0 {cache.parent.parent} {cache.parent}; rm -rf {cache} "
                            f"{release}/scripts/odd.pyc; chmod 0600 {older}/{marker}; "
                            f"mkdir -p {older}/scripts/__pycache__ && printf x > {older}/scripts/__pycache__/m.cpython-314.pyc")
            check(reset.returncode == 0 and caches_import().returncode == 0 and pyc.is_file(),
                  f"a real cache to begin with ({label}): {reset.stderr}")
            planted = as_root(plant)
            check(planted.returncode == 0, f"planting {label}: {planted.stderr}")
            done = install()
            check(done.returncode != 0 and message in done.stderr and "refusing to install; nothing was changed" in done.stderr,
                  f"{label} refuses the install: {done.stderr[-700:]}")
            check((older / "scripts" / "__pycache__" / "m.cpython-314.pyc").exists() and mode(older / marker) == 0o600,
                  f"and changes nothing first: the older release's safe cache and narrow marker stand ({label})")
            check(os.readlink(shared / "current") == before, f"current is untouched ({label})")
            check(not list(elsewhere.iterdir()), f"nothing was written through a link ({label})")
        as_root(f"rm -f {tmp}/second-name; chmod 0755 {release} {cache.parent.parent} {cache.parent}; "
                f"chown 0 {cache.parent.parent} {cache.parent}; rm -rf {cache} {release}/scripts/odd.pyc")
        done = install()
        check(done.returncode == 0 and not exact(), f"restored, it installs and is exact again: {done.stderr[-600:]} {exact()[:3]}")
        check(not called.exists(), "and no case ran systemctl")
    finally:
        as_root(f"rm -rf {tmp}")
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"release_bytecode_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
