#!/usr/bin/env python3
"""SYRD-754: a candidate publication moves its candidate ref and never the integration branch.

The real switchyard-publish-candidate, run as a role would run it, against a
real bare repository reached two ways: as a filesystem path (the way MEFP's
/data/git/fixpatch is reached) and as a `file://` URL (git's own transport, the
way a hosted remote is spoken to). Nothing is stubbed.

Cases, on both remotes: an ordinary publication and its --replace; a candidate
ref that is a symbolic ref to main (a push to it moves main -- measured below
before the tool is asked), and one that another ref aliases; the remote's HEAD
naming the candidate ref, or naming an integration branch outside the static
list; a commit that is already main's tip, or already under it, including when
the worktree does not hold main's tip; a retry of an already-published ref after
main was moved onto the commit; worktree hooks (pre-push and
reference-transaction) that would push main, which must not run; and a remote
that moves main itself when the candidate arrives, which must fail the run with
"Do NOT submit".
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "scripts" / "switchyard-publish-candidate"
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "TICKET_BOARD_URL": "", "TICKET_BOARD_CALLER_ROLE": ""}
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=ENV, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


def publish(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(PUBLISH), "--role", "app", *args], cwd=worktree,
                          capture_output=True, text=True, env=ENV, timeout=90)


class Remote:
    """A bare repository whose HEAD names main, and a role's clone of it, reached by `url`."""

    def __init__(self, root: Path, name: str, scheme: str):
        self.bare = root / f"{name}.git"
        git("init", "--bare", "-q", "-b", "main", str(self.bare))
        self.url = f"file://{self.bare}" if scheme == "file" else str(self.bare)
        seed = root / f"{name}-seed"
        git("init", "-q", "-b", "main", str(seed))
        (seed / "README").write_text("seed\n")
        git("add", "README", cwd=seed)
        git("commit", "-qm", "seed", cwd=seed)
        git("push", "-q", str(self.bare), "HEAD:refs/heads/main", cwd=seed)
        self.work = root / f"{name}-app"
        git("clone", "-q", self.url, str(self.work))
        self.count = 0

    def commit(self, parent: str | None = None) -> str:
        """A new commit in the role's worktree, on `parent` (its HEAD when None)."""
        self.count += 1
        if parent:
            git("checkout", "-q", "--detach", parent, cwd=self.work)
        (self.work / f"f{self.count}").write_text(f"{self.count}\n")
        git("add", f"f{self.count}", cwd=self.work)
        git("commit", "-qm", f"change {self.count}", cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def ref(self, name: str) -> str:
        done = subprocess.run(["git", "--git-dir", str(self.bare), "rev-parse", "--verify", "--quiet", name],
                              capture_output=True, text=True, env=ENV)
        return done.stdout.strip()

    def main(self) -> str:
        return self.ref("refs/heads/main")

    def set(self, name: str, commit: str) -> None:
        git("--git-dir", str(self.bare), "update-ref", "--no-deref", name, commit)

    def land(self, name: str, commit: str) -> None:
        """`name` on the remote moved to `commit`, pushed from the worktree that made it."""
        git("push", "-q", "--force", "--no-verify", str(self.bare), f"{commit}:{name}", cwd=self.work)

    def symbolic(self, name: str, target: str) -> None:
        git("--git-dir", str(self.bare), "symbolic-ref", name, target)

    def drop(self, name: str) -> None:
        git("--git-dir", str(self.bare), "update-ref", "--no-deref", "-d", name)


def refused(result: subprocess.CompletedProcess[str], *words: str) -> bool:
    text = result.stderr + result.stdout
    return result.returncode != 0 and all(word in text for word in words)


def measured_alias(root: Path) -> None:
    """The premise, measured on plain git: a push to a symbolic ref of main moves main."""
    remote = Remote(root, "premise", "path")
    tip = remote.main()
    remote.symbolic("refs/heads/roles/app/alias", "refs/heads/main")
    new = remote.commit(tip)
    git("push", "-q", remote.url, f"{new}:refs/heads/roles/app/alias", cwd=remote.work)
    check(remote.main() == new, "plain git: a push to a symbolic ref of main moves main, on a filesystem remote")


def cases(root: Path, scheme: str) -> None:
    remote = Remote(root, scheme, scheme)
    label = f"[{scheme}]"
    base = remote.main()

    # Ordinary publication, and its --replace: the candidate ref moves, main does not.
    first = remote.commit(base)
    done = publish(remote.work, "--commit", first, "--ref", "roles/app/t1")
    check(done.returncode == 0 and remote.ref("refs/heads/roles/app/t1") == first and remote.main() == base,
          f"{label} an ordinary candidate is published at its ref only: {done.stderr}")
    second = remote.commit(base)
    done = publish(remote.work, "--commit", second, "--ref", "roles/app/t1", "--replace")
    check(done.returncode == 0 and remote.ref("refs/heads/roles/app/t1") == second and remote.main() == base,
          f"{label} --replace moves the candidate ref only: {done.stderr}")
    again = publish(remote.work, "--commit", second, "--ref", "roles/app/t1")
    check(again.returncode == 0 and "nothing to push" in again.stdout and remote.main() == base,
          f"{label} a retry of a published ref pushes nothing and succeeds: {again.stdout} {again.stderr}")

    # A candidate ref that is a symbolic ref to main: refused before anything is pushed.
    remote.symbolic("refs/heads/roles/app/alias", "refs/heads/main")
    aliased = remote.commit(base)
    done = publish(remote.work, "--commit", aliased, "--ref", "roles/app/alias", "--replace")
    check(refused(done, "roles/app/alias is a symbolic ref on the remote, to refs/heads/main", "Nothing was pushed")
          and remote.main() == base, f"{label} a push through an alias of main is refused: {done.stderr}")
    remote.drop("refs/heads/roles/app/alias")
    # ...and a candidate ref another ref aliases: a push would move that one too.
    remote.set("refs/heads/roles/app/t2", base)
    remote.symbolic("refs/heads/roles/app/mirror", "refs/heads/roles/app/t2")
    done = publish(remote.work, "--commit", aliased, "--ref", "roles/app/t2", "--replace")
    check(refused(done, "refs/heads/roles/app/mirror on the remote is a symbolic ref to refs/heads/roles/app/t2")
          and remote.ref("refs/heads/roles/app/t2") == base,
          f"{label} an aliased candidate ref is refused: {done.stderr}")
    remote.drop("refs/heads/roles/app/mirror")

    # The remote's HEAD names the candidate ref: that ref is the integration branch now.
    remote.set("refs/heads/roles/app/trunk", base)
    remote.symbolic("HEAD", "refs/heads/roles/app/trunk")
    done = publish(remote.work, "--commit", aliased, "--ref", "roles/app/trunk", "--replace")
    check(refused(done, "roles/app/trunk is the remote's integration branch (its HEAD names it)")
          and remote.ref("refs/heads/roles/app/trunk") == base,
          f"{label} the branch HEAD names is refused, whatever it is called: {done.stderr}")
    # ...and a commit already on that non-static integration branch is refused like one on main.
    remote.land("refs/heads/roles/app/trunk", first)
    done = publish(remote.work, "--commit", first, "--ref", "roles/app/t3")
    check(refused(done, f"{first[:12]} is already on refs/heads/roles/app/trunk", "Do NOT submit")
          and not remote.ref("refs/heads/roles/app/t3"), f"{label} already on HEAD's branch: {done.stderr}")
    remote.symbolic("HEAD", "refs/heads/main")
    remote.drop("refs/heads/roles/app/trunk")

    # A commit already on main -- its tip, or under it -- reached it without the Publisher: refused.
    early = remote.commit(base)
    remote.land("refs/heads/main", early)
    done = publish(remote.work, "--commit", early, "--ref", "roles/app/t4")
    check(refused(done, f"{early[:12]} is already on refs/heads/main", "Only the Publisher puts a commit there",
                  "Do NOT submit") and not remote.ref("refs/heads/roles/app/t4"),
          f"{label} main's own tip is not published as a candidate: {done.stderr}")
    later = remote.commit(early)
    git("push", "-q", remote.url, f"{later}:refs/heads/main", cwd=remote.work)
    fresh = root / f"{scheme}-fresh"
    git("init", "-q", "-b", "elsewhere", str(fresh))
    git("remote", "add", "origin", remote.url, cwd=fresh)
    git("fetch", "-q", "origin", f"{early}:refs/heads/candidate", cwd=fresh)
    present = subprocess.run(["git", "cat-file", "-e", f"{later}^{{commit}}"], cwd=fresh, env=ENV, capture_output=True)
    check(present.returncode != 0, f"{label} the fresh worktree does not hold main's tip")
    done = publish(fresh, "--commit", early, "--ref", "roles/app/t5")
    check(refused(done, f"{early[:12]} is already on refs/heads/main") and not remote.ref("refs/heads/roles/app/t5"),
          f"{label} a commit under main is refused even when main's tip had to be fetched: {done.stderr}")
    tip = remote.main()

    # A retry of a published candidate after main was moved onto it: refused, not "nothing to push".
    retried = remote.commit(tip)
    done = publish(remote.work, "--commit", retried, "--ref", "roles/app/t6")
    check(done.returncode == 0, f"{label} published: {done.stderr}")
    remote.set("refs/heads/main", retried)
    done = publish(remote.work, "--commit", retried, "--ref", "roles/app/t6")
    check(refused(done, f"{retried[:12]} is already on refs/heads/main") and "nothing to push" not in done.stdout,
          f"{label} the already-published shortcut does not hide a main moved onto the commit: {done.stdout}")
    remote.set("refs/heads/main", tip)

    # Worktree hooks that would push main do not run.
    hooked = remote.commit(tip)  # made before the hooks exist: they would recurse through this test's own git
    hooks = remote.work / ".git" / "hooks"
    marker = root / f"{scheme}-hook-ran"
    for hook in ("pre-push", "reference-transaction"):
        (hooks / hook).write_text(f"#!/bin/sh\n[ -n \"$SYRD754_IN_HOOK\" ] && exit 0\necho {hook} >> {marker}\n"
                                  f"SYRD754_IN_HOOK=1 git push -q {remote.url} {hooked}:refs/heads/main "
                                  ">/dev/null 2>&1\nexit 0\n")
        (hooks / hook).chmod(0o755)
    done = publish(remote.work, "--commit", hooked, "--ref", "roles/app/t7")
    check(done.returncode == 0 and not marker.exists() and remote.main() == tip
          and remote.ref("refs/heads/roles/app/t7") == hooked,
          f"{label} no worktree hook runs during the push, so none can push main: {done.stderr}")
    for hook in ("pre-push", "reference-transaction"):
        (hooks / hook).unlink()

    # A remote that moves main itself when the candidate arrives: the run fails and says not to submit.
    receive = remote.bare / "hooks" / "post-receive"
    receive.write_text("#!/bin/sh\nwhile read old new ref; do git update-ref refs/heads/main \"$new\"; done\n")
    receive.chmod(0o755)
    moved = remote.commit(tip)
    done = publish(remote.work, "--commit", moved, "--ref", "roles/app/t8")
    check(refused(done, f"refs/heads/main on origin moved from {tip[:12]} to {moved[:12]} while this ran",
                  "Do NOT submit") and "published roles/app/t8" not in done.stdout,
          f"{label} main moved onto the candidate by the push is caught: {done.stderr} {done.stdout}")
    receive.unlink()
    remote.set("refs/heads/main", tip)
    # Main moved meanwhile by something unrelated to this commit is not this run's failure.
    unrelated = remote.commit(tip)
    remote.land("refs/heads/unrelated", unrelated)
    receive.write_text(f"#!/bin/sh\ncat >/dev/null\ngit update-ref refs/heads/main {unrelated}\n")
    receive.chmod(0o755)
    other = remote.commit(tip)
    done = publish(remote.work, "--commit", other, "--ref", "roles/app/t9")
    check(done.returncode == 0 and remote.main() == unrelated, f"{label} an unrelated move of main is not this "
                                                               f"candidate's failure: {done.stderr}")
    receive.unlink()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd754-pc-") as tmp:
        root = Path(tmp)
        measured_alias(root)
        for scheme in ("path", "file"):
            cases(root, scheme)
    print(f"publish_candidate_integration_guard_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
