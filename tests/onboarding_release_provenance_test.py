#!/usr/bin/env python3
"""An upgrade from an immutable release refreshes untouched onboarding docs, and nothing else (SYRD-683).

pie, 2026-10-25: `switchyard upgrade` from the installed release d24a15cf to
fd06cac8 skipped all five onboarding docs with "cannot verify source commit".
It looked for the old release's docs only in the tenant's `--commit-git-dir`,
pie's own `control.git`, which never holds a Switchyard commit. The old
release was installed all along.

Here two installed releases, old and new, are built the way the installer
lays them out: `<install root>/releases/<commit>`, each with its marker and
`docs/onboarding/`, and no `.git`. Two tenants, pie and otto, each have a
`control.git` with only its own commits, and copies stamped by the old release.
The upgrade is the real `upgrade_switchyard_onboarding_docs`, with a runner
that runs git and records everything else, as the launcher suites do.

`onboarding_provenance.TRUST` is where its proof starts and who must own each
component. This process cannot own `/`, so the first part moves the proof to
its own temporary tree and its own uid; one check keeps the real default and
is refused. The second part runs in a user namespace with two real identities
(namespace root, and uid 1), so a tenant's repository really belongs to
somebody else: there, the tenant's `control.git` holding the very Switchyard
commits is still not read, and a cache root keeps is.
"""

from __future__ import annotations

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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import onboarding_provenance  # noqa: E402
from scripts import project_onboarding  # noqa: E402

CHECKS = 0
NAMES = project_onboarding.SWITCHYARD_ONBOARDING_DOC_NAMES
OLD = hashlib.sha1(b"syrd-683 old release").hexdigest()
NEW = hashlib.sha1(b"syrd-683 new release").hexdigest()


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def body(tag: str, name: str) -> str:
    return (f"# {name}\n\nAs the {tag} release shipped it.\n\n- one rule\n- another rule, {tag}\n\n"
            f"Closing paragraph of {name}.\n")


def stamp(commit: str, name: str, text: str) -> str:
    return project_onboarding._stamp_onboarding_doc(source_name=name, source_commit=commit, body=text)


def git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def make_release(install: Path, commit: str, tag: str) -> Path:
    release = install / "releases" / commit
    docs = release / "docs" / "onboarding"
    docs.mkdir(parents=True)
    for name in NAMES:
        docs.joinpath(name).write_text(body(tag, name), encoding="utf-8")
    release.joinpath(".switchyard-release.json").write_text(json.dumps({"commit": commit}) + "\n", encoding="utf-8")
    for path in [release, *release.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    return release


class World:
    """Two installed releases and two tenants, in a directory of its own that the proof starts from."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.install = tmp / "opt" / "switchyard"
        self.old = make_release(self.install, OLD, "old")
        self.new = make_release(self.install, NEW, "new")
        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(self.install)
        self.calls: list[list[str]] = []
        self.tenants: dict[str, Path] = {}
        for slug in ("pie", "otto"):
            project = tmp / slug
            docs = project / "docs" / "onboarding"
            docs.mkdir(parents=True)
            for name in NAMES:
                docs.joinpath(name).write_text(stamp(OLD, name, body("old", name)), encoding="utf-8")
            work = tmp / f"{slug}-work"
            work.mkdir()
            git("init", "-q", "-b", "main", cwd=work)
            work.joinpath("app.txt").write_text(f"{slug}'s own project\n", encoding="utf-8")
            git("add", "app.txt", cwd=work)
            git("commit", "-q", "-m", f"{slug} work", cwd=work)
            git("clone", "-q", "--bare", str(work), str(project / "control.git"))
            self.tenants[slug] = project

    def runner(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:1] == ["git"]:
            return subprocess.run(args, **kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    def upgrade(self, slug: str, *, dry_run: bool, commit_git_dir: str | None = "") -> dict[str, str]:
        out: list[str] = []
        project = self.tenants[slug]
        project_onboarding.upgrade_switchyard_onboarding_docs(
            source_repo=self.new, project_dir=project, owner_user=current_user(),
            commit_git_dir=str(project / "control.git") if commit_git_dir == "" else commit_git_dir,
            dry_run=dry_run, runner=self.runner, print_func=out.append)
        said = {}
        for line in out:
            prefix = "switchyard: onboarding doc "
            if line.startswith(prefix) and ": " in line[len(prefix):]:
                name, outcome = line[len(prefix):].split(": ", 1)
                check(name not in said, f"one line per doc: {out}")
                said[name] = outcome
        return said

    def doc(self, slug: str, name: str) -> Path:
        return self.tenants[slug] / "docs" / "onboarding" / name


def current_user() -> str:
    from scripts import team_launcher

    return team_launcher.current_user_name()


def snapshot(directory: Path) -> dict[str, tuple[bytes, int, int]]:
    return {p.name: (p.read_bytes(), p.stat().st_ino, stat.S_IMODE(p.lstat().st_mode))
            for p in sorted(directory.iterdir()) if not p.is_symlink()}


def in_world(test) -> None:
    with tempfile.TemporaryDirectory(prefix="syrd683.") as raw:
        tmp = Path(raw)
        tmp.chmod(0o755)
        saved = onboarding_provenance.TRUST, os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT")
        onboarding_provenance.TRUST = (str(tmp), os.getuid())
        try:
            test(World(tmp))
        finally:
            onboarding_provenance.TRUST = saved[0]
            if saved[1] is None:
                os.environ.pop("SWITCHYARD_SHARED_INSTALL_ROOT", None)
            else:
                os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = saved[1]


def test_untouched_docs_refresh_from_the_installed_old_release(world: World) -> None:
    readme = world.doc("pie", "README.md")
    readme.write_text(readme.read_text(encoding="utf-8") + "\n## pie\n\nOur own section.\n", encoding="utf-8")
    world.doc("pie", "switchyard-board-guide.md").chmod(0o640)
    docs = world.tenants["pie"] / "docs" / "onboarding"
    before = snapshot(docs)
    planned = world.upgrade("pie", dry_run=True)
    check(snapshot(docs) == before, "a dry run writes nothing")
    old_docs = world.old / "docs" / "onboarding"
    for name in NAMES:
        if name == "README.md":
            check(planned[name] == f"skipped, edited since source commit {OLD} (compared with {old_docs / name})",
                  f"pie's README, with its own section, is kept, naming what it was compared with: {planned[name]}")
        else:
            check(planned[name] == f"would refresh to {NEW}, unedited since {OLD} (checked against {old_docs / name})",
                  f"an untouched doc would refresh, naming the installed release it was checked against: {planned}")
    applied = world.upgrade("pie", dry_run=False)
    check({n: o.replace("would refresh", "refreshed", 1) for n, o in planned.items()} == applied,
          f"the upgrade decides exactly as its dry run did: {planned} {applied}")
    after = snapshot(docs)
    for name in NAMES:
        if name == "README.md":
            check(after[name] == before[name], "the edited README is byte for byte as it was, the same file")
            continue
        check(after[name][0] == stamp(NEW, name, body("new", name)).encode(),
              f"{name} is the new release's copy, stamped with it")
        check(after[name][1] != before[name][1] and after[name][2] == before[name][2],
              f"{name} was replaced whole, a new file renamed over it, keeping its mode: {before[name][1:]} "
              f"{after[name][1:]}")
    check(sorted(after) == sorted(NAMES), f"nothing staged is left beside them: {sorted(after)}")
    check(not any("control.git" in " ".join(call) for call in world.calls),
          f"the tenant's own repository was never asked: {[c for c in world.calls if 'control.git' in ' '.join(c)]}")
    again = world.upgrade("pie", dry_run=False)
    check(all(again[n] == f"current at {NEW}" for n in NAMES if n != "README.md")
          and again["README.md"].startswith(f"skipped, edited since source commit {OLD}"),
          f"a second upgrade finds them current: {again}")
    check(snapshot(docs) == after, "and changes nothing, not even a file's identity")
    other = world.upgrade("otto", dry_run=False)
    check(all(outcome.startswith(f"refreshed to {NEW}") for outcome in other.values())
          and all(world.doc("otto", n).read_text(encoding="utf-8") == stamp(NEW, n, body("new", n)) for n in NAMES),
          f"a second tenant, whose control.git holds only its own commits, refreshes too: {other}")


def test_only_the_exact_shipped_copy_is_refreshed(world: World) -> None:
    name = "switchyard-board-guide.md"
    shipped = stamp(OLD, name, body("old", name))
    same_size = shipped.replace("one rule", "one rulE", 1)
    check(len(same_size) == len(shipped) and same_size != shipped, "the same-size edit is a real edit")
    header, rest = shipped.split("\n\n", 1)
    edited = f"skipped, edited since source commit {OLD}"
    variants = {
        "line endings changed to CRLF": (shipped.replace("\n", "\r\n").encode(), "skipped, no provenance header"),
        "a byte-order mark in front": (b"\xef\xbb\xbf" + shipped.encode(), "skipped, no provenance header"),
        "the body cut short (a prefix)": (shipped[:-30].encode(), edited),
        "the final newline dropped": (shipped[:-1].encode(), edited),
        "text appended": ((shipped + "More.\n").encode(), edited),
        "CRLF in the body only": ((header + "\n\n" + rest.replace("\n", "\r\n")).encode(), edited),
        "one character changed, same size": (same_size.encode(), edited),
        "the header with a doubled space": (shipped.replace("snapshot: source", "snapshot:  source", 1).encode(),
                                            "skipped, no provenance header"),
        "the header's commit in capitals": (shipped.replace(OLD, OLD.upper(), 1).encode(),
                                            f"{OLD.upper()!r} is not an exact release commit"),
        "the header naming another doc": (shipped.replace(f"docs/onboarding/{name}", "docs/onboarding/README.md",
                                                          1).encode(), "skipped, provenance names README.md"),
        "the header's blank line removed": ((header + "\n" + rest).encode(), "skipped, no provenance header"),
        "a newer header over the old body": (stamp(NEW, name, body("old", name)).encode(),
                                             f"skipped, edited since source commit {NEW}"),
        "a short commit": (shipped.replace(OLD, OLD[:12], 1).encode(), f"{OLD[:12]!r} is not an exact release commit"),
        "a path for a commit": (shipped.replace(OLD, f"../releases/{NEW}", 1).encode(),
                                f"'../releases/{NEW}' is not an exact release commit"),
    }
    target = world.doc("pie", name)
    for label, (raw, reason) in variants.items():
        target.write_bytes(raw)
        planned = world.upgrade("pie", dry_run=True)
        applied = world.upgrade("pie", dry_run=False)
        check(applied[name].startswith("skipped") and reason in applied[name] and applied[name] == planned[name]
              and target.read_bytes() == raw,
              f"{label}: kept, untouched, in a dry run and in the upgrade, because {reason!r}: {applied[name]}")
    target.write_bytes(shipped.encode())
    world.old.joinpath("docs", "onboarding", name).write_text(body("old", name) + "x", encoding="utf-8")
    said = world.upgrade("pie", dry_run=False)
    check(said[name].startswith(f"skipped, edited since source commit {OLD}") and target.read_bytes() == shipped.encode(),
          f"an old release whose copy differs is no proof, and nothing is overwritten: {said[name]}")


def test_a_release_shipping_crlf_is_compared_as_installation_read_it(world: World) -> None:
    name = "adversarial-collaborative-methodology.md"
    shipped_crlf = body("old", name).replace("\n", "\r\n")
    world.old.joinpath("docs", "onboarding", name).write_bytes(shipped_crlf.encode())
    installed = stamp(OLD, name, body("old", name))  # what `switchyard new` wrote: read_text turned CRLF into LF
    world.doc("pie", name).write_text(installed, encoding="utf-8")
    said = world.upgrade("pie", dry_run=False)
    check(said[name].startswith(f"refreshed to {NEW}, unedited since {OLD}")
          and world.doc("pie", name).read_text(encoding="utf-8") == stamp(NEW, name, body("new", name)),
          f"a copy installed from a CRLF release is recognised as untouched: {said[name]}")


def broken_sources(world: World) -> list[tuple[str, object, str]]:
    """Each way the old release can be unusable: (label, how to break it, what the message must say)."""
    old, name = world.old, "README.md"
    docs = old / "docs" / "onboarding"

    def replace_with_link(path: Path, *, directory: bool) -> None:
        moved = world.tmp / f"elsewhere-{path.name}"
        shutil.move(str(path), str(moved))
        path.symlink_to(moved, target_is_directory=directory)

    def fifo() -> None:
        docs.joinpath(name).unlink()
        os.mkfifo(docs / name)

    return [
        ("the old release not installed", lambda: shutil.rmtree(old), f"{old} does not exist"),
        ("its marker naming another release", lambda: old.joinpath(".switchyard-release.json").write_text(
            json.dumps({"commit": NEW})), f"{old} records {NEW}, not {OLD}"),
        ("its marker not JSON", lambda: old.joinpath(".switchyard-release.json").write_text("{"), "cannot be read"),
        ("the release a link to a copy", lambda: replace_with_link(old, directory=True), f"{old} is a symbolic link"),
        ("its docs a link", lambda: replace_with_link(old / "docs", directory=True), f"{old}/docs is a symbolic link"),
        ("the doc a link", lambda: replace_with_link(docs / name, directory=False), f"{docs / name} is a symbolic link"),
        ("the doc missing", lambda: docs.joinpath(name).unlink(), f"{docs / name} does not exist"),
        ("the doc a FIFO", fifo, f"{docs / name} is not a regular file"),
        ("its docs writable by others", lambda: docs.chmod(0o777), f"{docs} is writable by its group or by others"),
        ("the releases directory writable by its group", lambda: (world.install / "releases").chmod(0o775),
         f"{world.install}/releases is writable by its group or by others"),
    ]


def test_an_unusable_old_release_is_named_and_nothing_is_written(world: World) -> None:
    for label, _break, _said in broken_sources(world):
        in_world(lambda fresh, label=label: _one_broken_source(fresh, label))


def _one_broken_source(world: World, label: str) -> None:
    breaker, said = next((b, s) for lab, b, s in broken_sources(world) if lab == label)
    name = "README.md"
    before = world.doc("pie", name).read_bytes()
    breaker()
    planned = world.upgrade("pie", dry_run=True)
    applied = world.upgrade("pie", dry_run=False)
    control = world.tenants["pie"] / "control.git"
    check(applied[name].startswith(f"skipped, cannot verify source commit {OLD}: ") and said in applied[name]
          and f"source cache {control} does not hold {OLD}, {NEW}" in applied[name]
          and planned[name] == applied[name] and world.doc("pie", name).read_bytes() == before,
          f"{label}: skipped, naming the release and the cache it looked at, and nothing written: {applied[name]} "
          f"(dry run: {planned[name]}; unchanged: {world.doc('pie', name).read_bytes() == before})")


def test_unknown_commits_and_planted_copies_are_refused(world: World) -> None:
    name = "switchyard-director-guide.md"
    unknown = hashlib.sha1(b"never installed").hexdigest()
    target = world.doc("pie", name)
    target.write_text(stamp(unknown, name, body("old", name)), encoding="utf-8")
    said = world.upgrade("pie", dry_run=False, commit_git_dir=None)
    check(said[name] == (f"skipped, cannot verify source commit {unknown}: {world.install}/releases/{unknown} does "
                         "not exist; no source cache was named in --commit-git-dir"),
          f"a commit with no installed release and no cache named is said plainly: {said[name]}")
    real = world.tmp / "planted.md"
    real.write_text(stamp(OLD, name, body("old", name)), encoding="utf-8")
    target.unlink()
    target.symlink_to(real)
    said = world.upgrade("pie", dry_run=False)
    check(said[name] == "skipped, installed copy is a symbolic link" and target.is_symlink()
          and real.read_text(encoding="utf-8") == stamp(OLD, name, body("old", name)),
          f"a link the tenant planted at a doc is neither read nor written through: {said[name]}")


def test_the_real_default_trusts_root_alone(world: World) -> None:
    onboarding_provenance.TRUST = ("/", 0)
    name = "README.md"
    before = world.doc("otto", name).read_bytes()
    said = world.upgrade("otto", dry_run=False)
    first = Path(world.tmp.parts[0], world.tmp.parts[1])  # the first directory below / on the way to this world
    check(said[name].startswith(f"skipped, cannot verify source commit {OLD}: ")
          and ("not root" in said[name] or "writable by its group or by others" in said[name])
          and world.doc("otto", name).read_bytes() == before,
          f"with the real default, a release tree that is not root's is no proof ({first}...): {said[name]}")
    if os.stat("/").st_uid != 0:
        print("onboarding_release_provenance_test: the real installed release check SKIPPED: / is not root's here")
        return
    releases = Path("/opt/switchyard/releases")
    installed = sorted(p for p in releases.iterdir() if onboarding_provenance.FULL_COMMIT.fullmatch(p.name)) \
        if releases.is_dir() else []
    if not installed:
        print("onboarding_release_provenance_test: the real installed release check SKIPPED: none installed here")
        return
    os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = "/opt/switchyard"
    commit = installed[-1].name
    text, where, problem = onboarding_provenance.release_doc(commit, "switchyard-board-guide.md")
    shipped = installed[-1].joinpath("docs", "onboarding", "switchyard-board-guide.md").read_text(encoding="utf-8")
    check(problem == "" and text == shipped, f"a real installed release is read under the real default: {problem}")


def test_root_replaces_by_descriptor_keeping_the_mode(world: World) -> None:
    from scripts import owner_writes

    recorded = []
    saved = owner_writes.root_writes_for, owner_writes.write_owner_file
    owner_writes.root_writes_for = lambda _owner: True
    owner_writes.write_owner_file = lambda owner, path, text, **kw: recorded.append((owner, path, kw)) or True
    try:
        project_onboarding._replace_onboarding_doc("pie-agent", world.doc("pie", "README.md"), "x", runner=world.runner)
    finally:
        owner_writes.root_writes_for, owner_writes.write_owner_file = saved
    check(recorded == [("pie-agent", world.doc("pie", "README.md"), {"mode": None, "new_mode": 0o644})],
          f"as root the refresh is the descriptor write that keeps the replaced file's mode: {recorded}")


# -- The namespace part: two real identities, so a tenant's repository is really somebody else's. ------------------

def namespace_part() -> None:
    """Run as namespace root with uid 1 mapped: the tenant is uid 1, root is 0, and TRUST is root's own."""
    with tempfile.TemporaryDirectory(prefix="syrd683ns.") as raw:
        tmp = Path(raw)
        tmp.chmod(0o755)
        onboarding_provenance.TRUST = (str(tmp), 0)  # only the base moves: the owner is the real default
        install = tmp / "opt" / "switchyard"
        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(install)
        history = tmp / "history"
        history.mkdir()
        git("init", "-q", "-b", "main", cwd=history)
        commits = {}
        for tag in ("old", "new", "future"):
            for name in NAMES:
                history.joinpath("docs", "onboarding").mkdir(parents=True, exist_ok=True)
                history.joinpath("docs", "onboarding", name).write_text(body(tag, name), encoding="utf-8")
            git("add", "docs", cwd=history)
            git("commit", "-q", "-m", tag, cwd=history)
            commits[tag] = git("rev-parse", "HEAD", cwd=history)
        old, new, future = commits["old"], commits["new"], commits["future"]
        release = make_release(install, new, "new")  # only the new release is installed: the old one is gone
        root_cache = tmp / "var" / "lib" / "switchyard" / "source.git"
        git("clone", "-q", "--bare", str(history), str(root_cache))
        # A replace ref in it may not change what the old commit shipped.
        git("--git-dir", str(root_cache), "replace", old, future)
        project = tmp / "pie"
        docs = project / "docs" / "onboarding"
        docs.mkdir(parents=True)
        for name in NAMES:
            docs.joinpath(name).write_text(stamp(old, name, body("old", name)), encoding="utf-8")
        docs.joinpath("switchyard-librarian-guide.md").write_text(
            stamp(future, "switchyard-librarian-guide.md", body("future", "switchyard-librarian-guide.md")),
            encoding="utf-8")
        tenant_git = project / "control.git"
        git("clone", "-q", "--bare", str(history), str(tenant_git))
        for path in [project, *project.rglob("*")]:
            os.lchown(path, 1, 1)
        calls: list[list[str]] = []

        def runner(args, **kwargs):
            calls.append(list(args))
            return subprocess.run(args, **kwargs) if args[:1] == ["git"] else subprocess.CompletedProcess(args, 0)

        def upgrade(commit_git_dir: str) -> dict[str, str]:
            out: list[str] = []
            project_onboarding.upgrade_switchyard_onboarding_docs(
                source_repo=release, project_dir=project, owner_user=current_user(), commit_git_dir=commit_git_dir,
                dry_run=True, runner=runner, print_func=out.append)
            return {line.split(": ", 2)[1].removeprefix("onboarding doc "): line.split(": ", 2)[2]
                    for line in out if line.startswith("switchyard: onboarding doc ")}

        name = "README.md"
        said = upgrade(str(tenant_git))
        check(said[name].startswith(f"skipped, cannot verify source commit {old}: ")
              and f"source cache {tenant_git} is not root's, so it is not read as Switchyard history: {project} "
                  "belongs to bin, not root" in said[name]
              and not any(str(tenant_git) in " ".join(call) for call in calls),
              f"the tenant's control.git holds the very commits, and is still never read: {said[name]}")
        said = upgrade(f"{tenant_git}{os.pathsep}{root_cache}")
        check(said[name] == (f"would refresh to {new}, unedited since {old} (checked against source cache {root_cache} "
                             f"at {old})"),
              f"a cache root keeps is the fallback when the old release is gone: {said[name]}")
        check(said["switchyard-librarian-guide.md"] == "skipped, installed copy is newer than this checkout",
              f"and there the ancestry check still holds a newer copy back: {said['switchyard-librarian-guide.md']}")
        check(any(call[:3] == ["git", "--no-replace-objects", f"--git-dir={root_cache}"] and "show" in call
                  for call in calls),
              "the cache is read with replace refs off, so the planted replacement changed nothing")
        os.lchown(root_cache / "refs", 1, 1)
        said = upgrade(str(root_cache))
        check(f"{root_cache}/refs belongs to bin, not root" in said[name] and said[name].startswith("skipped"),
              f"a cache whose refs somebody else can rewrite is not root's: {said[name]}")
        for path in [project, *project.rglob("*")]:
            os.lchown(path, 0, 0)  # so the directory can be removed


def run_namespace_part() -> str:
    if shutil.which("unshare") is None:
        return "SKIPPED: unshare is not installed"
    probe = subprocess.run(["unshare", "--map-root-user", "--map-auto", "true"], capture_output=True, text=True)
    if probe.returncode != 0:
        return f"SKIPPED: no user namespace with a second identity here ({probe.stderr.strip()})"
    done = subprocess.run(["unshare", "--map-root-user", "--map-auto", sys.executable, "-B", __file__, "--namespace"],
                          capture_output=True, text=True, timeout=300)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    check(done.returncode == 0 and "namespace part: ok" in done.stdout,
          f"the namespace part passed: {done.stdout[-400:]} {done.stderr[-2000:]}")
    return "ran"


def main() -> int:
    if sys.argv[1:] == ["--namespace"]:
        namespace_part()
        print(f"namespace part: ok ({CHECKS} checks)")
        return 0
    for test in (test_untouched_docs_refresh_from_the_installed_old_release, test_only_the_exact_shipped_copy_is_refreshed,
                 test_a_release_shipping_crlf_is_compared_as_installation_read_it,
                 test_an_unusable_old_release_is_named_and_nothing_is_written,
                 test_unknown_commits_and_planted_copies_are_refused, test_the_real_default_trusts_root_alone,
                 test_root_replaces_by_descriptor_keeping_the_mode):
        in_world(test)
    namespace = run_namespace_part()
    print(f"onboarding_release_provenance_test: ok ({CHECKS} checks; namespace part {namespace})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
