#!/usr/bin/env python3
"""SYRD-625: the Publisher publishes approved candidates mechanically, on real boards and real git.

Production-built boards (schema.sql, companion roles, rbac.sql, the real
ticket-board-migrate, rbac.sql again), the real HTTP server and the real notify
listener with an injected sender. The forge is a real bare repository whose
HEAD names `main`; the board's trusted commit cache is a real bare repository
whose `origin` is that forge; implementers publish candidates to it at
`roles/<role>/...` the way switchyard-publish-candidate does. Nothing is
stubbed but, where a case needs one, the answer git gives to one command (a
refused credential, a dropped connection), injected through the Publisher's
git runner around the real git.

Cases: the ordinary publication into Final Sign-Off and towards User Review;
every lineage return; every Director escalation; every mechanical repair; the
crash points; the idempotent re-run; the lease race; the gates; the guard; the
listener pass; a board without the Publisher; and a board upgraded from main's
tree by this tree's migration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402
from router_role_board_test import Board, fixture, temporary_cluster  # noqa: E402
from scripts.ticket_board import publisher, publisher_pass  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher  # noqa: E402

#: Main's commit this ticket is cut from: the upgrade case's before-tree.
BEFORE = "41dd7c84d68152f086fc7d9bfee5416f1c0cd9a3"
CHECKS = 0
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def git(*args: str, cwd: Path | None = None, stdin: str | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}, check=True)
    return done.stdout.strip()


class World:
    """A forge, the board's trusted cache of it, and an implementer's clone to make candidates in."""

    def __init__(self, root: Path):
        self.root = root
        self.public = root / "forge" / "project.git"
        self.cache = root / "cache.git"
        self.dev = root / "dev"
        self.state = root / "publisher-state"
        self.public.parent.mkdir(parents=True)
        git("init", "--bare", "-q", "-b", "main", str(self.public))
        git("init", "-q", "-b", "main", str(self.dev))
        self.base = self.commit([], {"README.md": "project\n", "src/app.py": "print('app')\n"}, "base")
        git("push", "-q", str(self.public), f"{self.base}:refs/heads/main", cwd=self.dev)
        git("init", "--bare", "-q", str(self.cache))
        git("--git-dir", str(self.cache), "remote", "add", "origin", str(self.public))
        self.refresh_cache()

    def commit(self, parents: list[str], files: dict[str, str | None], message: str) -> str:
        """A commit on `parents` writing each file (or removing it, None), made with plumbing in the clone."""
        index = self.root / "dev.index"
        env = {**os.environ, **GIT_ENV, "GIT_INDEX_FILE": str(index)}

        def run(*args: str, stdin: str | None = None) -> str:
            return subprocess.run(["git", "-C", str(self.dev), *args], input=stdin, capture_output=True, text=True,
                                  env=env, check=True).stdout.strip()

        run("read-tree", parents[0]) if parents else run("read-tree", "--empty")
        for name, text in files.items():
            if text is None:
                run("update-index", "--force-remove", name)
                continue
            blob = run("hash-object", "-w", "--stdin", stdin=text)
            run("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
        tree = run("write-tree")
        return run("commit-tree", tree, *[a for p in parents for a in ("-p", p)], "-m", message)

    def publish(self, ref: str, commit: str) -> None:
        """What switchyard-publish-candidate does: push the ref, then bring the board's cache up to it."""
        git("push", "-q", "--force", str(self.public), f"{commit}:refs/heads/{ref}", cwd=self.dev)
        git("--git-dir", str(self.cache), "fetch", "-q", "origin", f"+refs/heads/{ref}:refs/remotes/origin/{ref}")

    def land_on_main(self, commit: str) -> None:
        """Somebody else's integration: public main moves to `commit`."""
        git("push", "-q", str(self.public), f"{commit}:refs/heads/main", cwd=self.dev)

    def refresh_cache(self) -> None:
        git("--git-dir", str(self.cache), "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")

    def main(self) -> str:
        return git("--git-dir", str(self.public), "rev-parse", "refs/heads/main")

    def cache_main(self) -> str:
        return git("--git-dir", str(self.cache), "rev-parse", "refs/remotes/origin/main")

    def obj(self, commit: str) -> str:
        """The commit object's exact bytes, as the cache holds them."""
        return git("--git-dir", str(self.cache), "cat-file", "commit", commit)


def example() -> dict:
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return cfg


def composed(cfg: dict) -> dict:
    """`with_publisher(cfg)`, where a refusal is a failed check rather than a crash."""
    try:
        return with_publisher(cfg)
    except ValueError as exc:
        check(False, f"the example composes with the Publisher: {exc}")
        raise


def runs(command: str) -> None:
    """A printed remedy, run exactly as printed."""
    done = subprocess.run(command.split(), capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    check(done.returncode == 0, f"the printed remedy runs as printed: {command}: {done.stderr}")


def short(answer: dict) -> str:
    text = answer.get("refused", "")
    return "ok" if "ok" in answer else "refused: " + (text if len(text) <= 700 else text[:400] + " ... " + text[-300:])


def row(board: Board, ticket: str) -> dict:
    return json.loads(board.sql(
        "SELECT json_build_object('state', state, 'assignee', assignee, 'commit', commit_hash, "
        "'audit', audit_signoff, 'inspector', inspector_signoff, 'user', user_signoff, 'flags', workflow_flags, "
        "'exempt', commit_exempt)::text "
        f"FROM ticket_board.tickets WHERE id = '{ticket}';"))


def comments(board: Board, ticket: str, who: str = "publisher") -> list[str]:
    return json.loads(board.sql("SELECT coalesce(json_agg(text ORDER BY id), '[]')::text FROM "
                                f"ticket_board.ticket_comments WHERE ticket_id = '{ticket}' AND who = '{who}';"))


def attempts(board: Board, ticket: str) -> list[dict]:
    return json.loads(board.sql(
        "SELECT coalesce(json_agg(json_build_object('state', state, 'step', step, 'candidate', candidate, "
        "'expected', expected_parent, 'repairs', repairs, 'facts', facts) ORDER BY id), '[]')::text "
        f"FROM ticket_board.publication_attempts WHERE ticket_id = '{ticket}';"))


class Runner:
    """The real git, with what a case needs said about some commands instead (and a count of each)."""

    def __init__(self, answer=None, after=None):
        self.answer = answer  # (argv) -> CompletedProcess | None (None: run the real git)
        self.after = after  # (argv, result) -> None, after the real git ran; may raise to stop the run there
        self.seen: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.seen.append(list(argv))
        canned = self.answer(argv) if self.answer else None
        if canned is not None:
            return canned
        result = subprocess.run(argv, **kwargs)
        if self.after:
            self.after(argv, result)
        return result

    def count(self, verb: str) -> int:
        return sum(1 for argv in self.seen if verb in argv[1:4])


def failed(argv, text: str, code: int = 128) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, code, "", text)


class Bench:
    """One board with the Publisher, its git world, and the ways the cases drive a ticket."""

    def __init__(self, cluster, name: str, root: Path, document: dict | None = None, *, before: Path | None = None):
        self.world = World(root / name)
        self.board = Board(cluster, name, document or composed(example()), before=before)
        self.board.app.commit_git_dirs = (self.world.cache,)
        self.board.app.commit_git_dir = self.world.cache
        self.slept: list[float] = []
        self.number = 100
        self.made: list[str] = []

    def settle(self) -> None:
        """Every earlier ticket put down, so its implementer is free: closed from Final Sign-Off, else deferred."""
        for t in self.made:
            now = row(self.board, t)
            if now["state"] == "director_review":
                answer = self.board.act(t, "mark_done", "director", commit_hash=now["commit"])
                check(short(answer) == "ok", f"the Director closes {t} on the published candidate: {short(answer)}")
            elif now["state"] not in ("done", "cancelled", "backlog"):
                check(short(self.board.act(t, "defer", "director")) == "ok", f"{t} deferred")
        self.made = []
        self.notices()

    def ticket(self, implementer: str = "app", *, files: dict | None = None, parents: list[str] | None = None,
               user_signoff: bool = False, publish: bool = True) -> tuple[str, str]:
        """A ticket routed, built, published, submitted and signed off by Audit. Returns (id, candidate)."""
        self.settle()
        self.number += 1
        t = f"PGU-{self.number}"
        self.made.append(t)
        fixture.seed_postgres_ticket(self.board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director")
        check(short(self.board.act(t, "route", "director", state="in_progress", assignee=implementer)) == "ok",
              f"{t} routed to {implementer}")
        if user_signoff:
            check(short(self.board.act(t, "set_workflow_flags", "director", needs_user_signoff=True)) == "ok",
                  f"{t} needs the User's sign-off")
        candidate = self.world.commit(parents or [self.world.main()], files or {f"src/{t.lower()}.py": f"# {t}\n"},
                                      f"{t}: the work")
        if publish:
            self.world.publish(f"roles/{implementer}/{t.lower()}", candidate)
        else:
            git("--git-dir", str(self.world.cache), "fetch", "-q", str(self.world.dev), candidate)
        answer = self.board.act(t, "submit_to_audit", implementer, commit_hash=candidate)
        check(short(answer) == "ok", f"{implementer} submits {t}: {short(answer)}")
        answer = self.board.act(t, "audit_sign_off", "audit")
        check(short(answer) == "ok", f"Audit signs off {t}: {short(answer)}")
        return t, candidate

    def publish(self, ticket: str, runner: Runner | None = None, repositories=None) -> dict:
        with psycopg.connect(self.board.listener_url, autocommit=True) as conn:
            return publisher.publish_ticket(
                conn, ticket, root=self.world.state, git=publisher.Git(runner=runner or Runner()),
                repositories=repositories or (lambda: [str(self.world.cache)]), sleep=self.slept.append)

    def attempt(self, ticket: str, runner: Runner | None = None) -> dict:
        """`publish`, where whatever the run raises is its answer rather than the end of the suite."""
        try:
            return self.publish(ticket, runner)
        except Exception as exc:  # noqa: BLE001 - an answer, checked by the case
            return {"outcome": f"raised: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"}

    def notices(self) -> list[tuple[str, str]]:
        """What the listener delivers now, to panes that have all finished their last turn."""
        self.board.busy.clear()
        return self.board.one_pass()

    def close(self) -> None:
        self.board.close()


def ordinary(bench: Bench) -> None:
    """Approved, published as a fast-forward, verified, recorded, and on to Final Sign-Off."""
    board, world = bench.board, bench.world
    t, candidate = bench.ticket()
    bench.notices()
    entered = row(board, t)
    check(entered["state"] == "publication" and entered["assignee"] == "publisher" and entered["audit"],
          f"Audit's approval lands in publication, with the Publisher, sign-off kept: {entered}")
    before_main, before_obj = world.main(), world.obj(candidate)
    result = bench.publish(t)
    check(result["outcome"] == "published" and result.get("recorded"), f"published and recorded: {result}")
    after = row(board, t)
    check(after["state"] == "director_review" and after["assignee"] == "director",
          f"without the User's sign-off it goes on to the Director's Final Sign-Off: {after}")
    check(after["commit"] == candidate and after["audit"] and after["flags"] == entered["flags"],
          f"the candidate and every sign-off are exactly what was approved: {after} vs {entered}")
    check(world.main() == candidate and git("--git-dir", str(world.public), "rev-parse", f"{candidate}^") == before_main,
          "public main is the candidate, one fast-forward on the tip it was built on")
    check(world.cache_main() == candidate, "the board's trusted cache reads it too")
    check(world.obj(candidate) == before_obj, "the commit published is byte for byte the commit approved")
    said = comments(board, t)
    check(len(said) == 1 and said[0].startswith(f"Published {candidate}") and "compare-and-swap leased" in said[0]
          and "trusted cache reads" in said[0], f"one evidence comment, by the Publisher: {said}")
    journal = attempts(board, t)
    check([(a["state"], a["step"]) for a in journal] == [("published", "verified")]
          and journal[0]["expected"] == before_main and journal[0]["facts"]["bundle_sha256"],
          f"one attempt, journalled to verified, with its lease and bundle: {journal}")
    sent = bench.notices()
    check([target for target, _ in sent] == ["pgu-director:0.0"],
          f"the Director, and only the Director, is told it is waiting for Final Sign-Off: {sent}")


def toward_user_review(bench: Bench) -> None:
    """A ticket that needs the User's sign-off goes on towards User Review, through Director acceptance as before."""
    t, candidate = bench.ticket(user_signoff=True)
    result = bench.publish(t)
    after = row(bench.board, t)
    check(result["outcome"] == "published" and after["state"] == "dat" and after["assignee"] == "director"
          and bench.world.main() == candidate,
          f"published, and on to Director acceptance, the board's way to User Review: {after}")


def returned(bench: Bench, t: str, *words: str) -> str:
    """The case's return: to the implementer, sign-offs and commit cleared, said once, nothing else told."""
    bench.notices()
    result = bench.publish(t)
    after = row(bench.board, t)
    said = comments(bench.board, t)
    check(result["outcome"] == "returned" and result.get("recorded"), f"{t} returned: {result}")
    check(after["state"] == "in_progress" and after["assignee"] == "app" and not after["audit"] and not after["user"]
          and not after["inspector"] and after["commit"] == "",
          f"{t} is back with its implementer, every sign-off and the commit cleared: {after}")
    check(len(said) == 1 and said[0].startswith("Publisher returned this ticket (") and all(w in said[0] for w in words)
          and "Nothing was pushed by this attempt" in said[0],
          f"{t}: one comment, with the exact diagnostic {words}: {said}")
    check([a["state"] for a in attempts(bench.board, t)] == ["returned"], f"{t}'s attempt ended returned")
    sent = bench.notices()
    check([target for target, _ in sent] == ["pgu-app:0.0"], f"{t}: the implementer alone is told: {sent}")
    return said[0]


def lineage(bench: Bench) -> None:
    """Every candidate the Publisher cannot fast-forward goes back to its implementer, once, saying why."""
    world, board = bench.world, bench.board
    # Main advanced past the candidate's parent, touching the same file.
    parent = world.main()
    t, candidate = bench.ticket(files={"src/app.py": "print('mine')\n"})
    theirs = world.commit([world.main()], {"src/app.py": "print('theirs')\n", "docs/x.md": "x\n"}, "someone else")
    world.land_on_main(theirs)
    returned(bench, t, "public main advanced", f"built on {parent[:12]}", f"now {theirs[:12]}, 1 commit(s) later",
             "Paths both changed: src/app.py.")
    check(world.main() == theirs, "public main is exactly what somebody else put there")
    # Renewed gates: the rebuilt candidate goes through Audit again before the Publisher sees it.
    rebuilt = world.commit([theirs], {"src/app.py": "print('mine, rebuilt')\n"}, f"{t}: rebuilt")
    world.publish(f"roles/app/{t.lower()}", rebuilt)
    check(short(board.act(t, "submit_to_audit", "app", commit_hash=rebuilt)) == "ok", "the rebuilt candidate submits")
    check(row(board, t)["state"] == "audit", "and is in Audit again: no sign-off survived the return")
    check(short(board.act(t, "audit_sign_off", "audit")) == "ok" and bench.publish(t)["outcome"] == "published"
          and world.main() == rebuilt, "signed off again, it is published")
    # Two commits on the tip, a merge, and a parent that is not on main at all.
    first = world.commit([world.main()], {"src/one.py": "1\n"}, "first of two")
    t, candidate = bench.ticket(parents=[first])
    returned(bench, t, f"is 2 commits on public main ({world.main()[:12]})", "squash it onto")
    side = world.commit([world.main()], {"src/side.py": "s\n"}, "side")
    t, candidate = bench.ticket(parents=[world.main(), side])
    returned(bench, t, "has 2 parents", "never merges")
    sibling = world.commit([git("--git-dir", str(world.public), "rev-parse", "main^")], {"src/sib.py": "s\n"}, "sibling")
    t, candidate = bench.ticket(parents=[sibling])
    returned(bench, t, "history differs from public main", f"its parent {sibling[:12]} is not on main")
    # Published nowhere: the board took it from the cache, and the cache has since lost it.
    t, candidate = bench.ticket(publish=False)
    forget(world, candidate)
    returned(bench, t, f"candidate {candidate} is neither in the board's trusted commit cache nor on",
             "Publish it (switchyard-publish-candidate)")
    check(world.main() == rebuilt, "no return moved public main")


def forget(world: World, commit: str) -> None:
    """The trusted cache loses `commit`: every ref to it gone and its objects pruned."""
    for ref in git("--git-dir", str(world.cache), "for-each-ref", "--format=%(refname)", "--contains", commit).split():
        git("--git-dir", str(world.cache), "update-ref", "-d", ref)
    (world.cache / "FETCH_HEAD").unlink(missing_ok=True)
    git("--git-dir", str(world.cache), "reflog", "expire", "--expire=now", "--all")
    git("--git-dir", str(world.cache), "repack", "-a", "-d", "-q")
    git("--git-dir", str(world.cache), "prune", "--expire=now")
    gone = subprocess.run(["git", "--git-dir", str(world.cache), "cat-file", "-e", commit], capture_output=True)
    check(gone.returncode != 0, f"the cache no longer has {commit[:12]}")


def escalated(bench: Bench, t: str, kind: str, runner: Runner | None = None, *words: str, repositories=None,
              moved: bool = False) -> str:
    """The case's escalation: to the Director, in publication_failed, said once, the implementer not told."""
    board, world = bench.board, bench.world
    bench.notices()
    before = row(board, t)
    main_before = world.main()
    earlier = len(comments(board, t))
    result = bench.publish(t, runner, repositories)
    after = row(board, t)
    said = comments(board, t)[earlier:]
    check(result["outcome"] == "escalated" and result.get("recorded"), f"{t} escalated: {result}")
    check(after["state"] == "publication_failed" and after["assignee"] == "director"
          and {k: after[k] for k in ("commit", "audit", "flags")} == {k: before[k] for k in ("commit", "audit", "flags")},
          f"{t} is with the Director, candidate and sign-offs untouched: {after} vs {before}")
    check(len(said) == 1 and said[0].startswith(f"Publisher could not publish this ticket ({kind}): ")
          and all(w in said[0] for w in words) and "publication_retry" in said[0],
          f"{t}: one comment naming {kind} and {words}: {said}")
    check(moved or world.main() == main_before, f"{t}: public main did not move")
    check([a["state"] for a in attempts(board, t)][-1] == "escalated", f"{t}'s attempt ended escalated")
    sent = bench.notices()
    check([target for target, _ in sent] == ["pgu-director:0.0"], f"{t}: the Director alone is told: {sent}")
    return said[0]


def retry(bench: Bench, t: str, candidate: str) -> None:
    """The Director's retry hands the ticket back to the Publisher, which publishes it as a new attempt."""
    check(short(bench.board.act(t, "publication_retry", "director")) == "ok", f"the Director retries {t}")
    check(row(bench.board, t)["state"] == "publication" and row(bench.board, t)["assignee"] == "publisher",
          f"{t} is the Publisher's again")
    result = bench.publish(t)
    check(result["outcome"] == "published" and bench.world.main() == candidate
          and row(bench.board, t)["state"] == "director_review", f"{t} published on the retry: {result}")


def director_failures(bench: Bench) -> None:
    """Credential, policy, transient, authority and identity failures are the Director's, each once."""
    world, board = bench.world, bench.board
    permission = "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository."
    t, candidate = bench.ticket()
    runner = Runner(lambda argv: failed(argv, permission) if "push" in argv else None)
    said = escalated(bench, t, "credential", runner, "Permission denied (publickey)",
                     "Nothing was pushed by this attempt", "The trusted cache names no push URL")
    check(runner.count("push") == 1, "a refused credential is not retried")
    remedy = said.split("`", 1)[1].split("`", 1)[0].replace("<SSH repository URL>", str(world.public))
    runs(remedy)
    check(git("--git-dir", str(world.cache), "config", "remote.origin.pushurl") == str(world.public),
          f"the printed remedy runs as printed: {remedy}")
    git("--git-dir", str(world.cache), "config", "--unset", "remote.origin.pushurl")
    retry(bench, t, candidate)
    # A hook on the forge refuses the branch.
    hook = world.public / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n  if [ \"$ref\" = refs/heads/main ]; then\n"
                    "    echo 'GH006: Protected branch update failed for refs/heads/main.' >&2; exit 1\n  fi\ndone\n")
    hook.chmod(0o755)
    t, candidate = bench.ticket()
    escalated(bench, t, "policy", None, "GH006: Protected branch update failed", "pre-receive hook declined")
    hook.unlink()
    retry(bench, t, candidate)
    # Transient failures: retried, bounded, and then the Director's.
    t, candidate = bench.ticket()
    dropped = "fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com"
    runner = Runner(lambda argv: failed(argv, dropped) if "push" in argv else None)
    slept = len(bench.slept)
    escalated(bench, t, "transient", runner, f"kept failing after {publisher.TRANSIENT_TRIES} tries",
              "Could not resolve host")
    check(runner.count("push") == publisher.TRANSIENT_TRIES and bench.slept[slept:] == [2.0, 5.0],
          f"tried exactly {publisher.TRANSIENT_TRIES} times, with bounded waits: {bench.slept[slept:]}")
    check(short(board.act(t, "publication_kick_back", "director", reason="the forge is gone; rebuild elsewhere")) == "ok"
          and row(board, t)["state"] == "in_progress" and not row(board, t)["audit"],
          "the Director can return a failed publication to the implementer instead, sign-offs cleared")
    # Authority: the board must name one repository, with an origin, whose HEAD names a branch.
    t, candidate = bench.ticket()
    escalated(bench, t, "authority", None, "verifies commits against 2 repositories",
              repositories=lambda: [str(world.cache), str(world.cache)])
    check(short(board.act(t, "publication_retry", "director")) == "ok", "retried")
    bare = world.root / "no-origin.git"
    git("clone", "-q", "--bare", str(world.cache), str(bare))
    git("--git-dir", str(bare), "remote", "remove", "origin")
    said = escalated(bench, t, "authority", None, "names no usable origin URL", repositories=lambda: [str(bare)])
    remedy = said.split("set it with `", 1)[1].split("`", 1)[0].replace("<repository URL>", str(world.public))
    runs(remedy)
    check(short(board.act(t, "publication_retry", "director")) == "ok", "retried")
    check(bench.publish(t, None, lambda: [str(bare)])["outcome"] == "published" and world.main() == candidate,
          f"the printed remedy, run as printed, is the fix: {remedy}")
    t, candidate = bench.ticket()
    git("--git-dir", str(world.public), "symbolic-ref", "HEAD", "refs/heads/nothing-here")
    escalated(bench, t, "authority", None, "does not name an integration branch")
    git("--git-dir", str(world.public), "symbolic-ref", "HEAD", "refs/heads/main")
    # A push target that is not the public repository: the read-back through the public URL catches it.
    decoy = world.root / "decoy.git"
    git("clone", "-q", "--bare", str(world.public), str(decoy))
    git("--git-dir", str(world.cache), "config", "remote.origin.pushurl", str(decoy))
    check(short(board.act(t, "publication_retry", "director")) == "ok", "retried")
    escalated(bench, t, "publication", None, f"reads {world.main()[:12]}, not {candidate[:12]}",
              "after a push git reported as successful", f"The push reported success, but public main does not "
                                                         f"carry {candidate[:12]}")
    git("--git-dir", str(world.cache), "config", "--unset", "remote.origin.pushurl")
    retry(bench, t, candidate)


def repairs(bench: Bench) -> None:
    """Each mechanical repair, and proof that none of them changes the candidate, its parent, or any gate."""
    world, board = bench.world, bench.board
    # A stale trusted cache: main moved and the cache was not told.
    theirs = world.commit([world.main()], {"src/theirs.py": "t\n"}, "someone else, earlier")
    world.land_on_main(theirs)
    t, candidate = bench.ticket()
    stale = world.cache_main()
    check(stale != theirs, "the cache's main is behind the forge")
    entered, body = row(board, t), world.obj(candidate)
    result = bench.publish(t)
    check(result["outcome"] == "published" and world.main() == candidate, f"published: {result}")
    said = comments(board, t)[0]
    check(f"refreshed the trusted cache's main from {stale[:12]} to {theirs[:12]}" in said,
          f"the stale cache was repaired, and said so: {said}")
    unchanged(bench, t, candidate, entered, body)
    # A candidate the cache lost but the forge has: fetched back by its SHA.
    t, candidate = bench.ticket()
    entered, body = row(board, t), world.obj(candidate)
    forget(world, candidate)
    result = bench.publish(t)
    said = comments(board, t)[0]
    check(result["outcome"] == "published" and f"fetched candidate {candidate[:12]} into the trusted cache" in said,
          f"the candidate was fetched back by its SHA, and published: {said}")
    unchanged(bench, t, candidate, entered, body)
    # A transient failure that clears: retried, and recorded as a repair.
    t, candidate = bench.ticket()
    entered, body = row(board, t), world.obj(candidate)
    tries = {"push": 0}

    def flaky(argv):
        if "push" in argv:
            tries["push"] += 1
            if tries["push"] < 3:
                return failed(argv, "fatal: the remote end hung up unexpectedly")
        return None

    result = bench.publish(t, Runner(flaky))
    said = comments(board, t)[0]
    check(result["outcome"] == "published" and tries["push"] == 3 and said.count("retried the push") == 1,
          f"two dropped pushes, then published, the retry recorded: {said}")
    unchanged(bench, t, candidate, entered, body)
    # A scratch repository git cannot read is rebuilt.
    work = world.state / "work.git"
    (work / "HEAD").write_text("garbage\n")
    t, candidate = bench.ticket()
    entered, body = row(board, t), world.obj(candidate)
    result = bench.publish(t)
    check(result["outcome"] == "published" and "rebuilt the Publisher's scratch repository" in comments(board, t)[0],
          f"the scratch repository was rebuilt: {comments(board, t)}")
    unchanged(bench, t, candidate, entered, body)


def unchanged(bench: Bench, t: str, candidate: str, entered: dict, body: str) -> None:
    after = row(bench.board, t)
    check(after["commit"] == candidate and after["audit"] == entered["audit"] and after["flags"] == entered["flags"]
          and after["inspector"] == entered["inspector"] and after["user"] == entered["user"],
          f"{t}: the repair changed no commit, gate or sign-off: {after} vs {entered}")
    check(bench.world.obj(candidate) == body and bench.world.main() == candidate
          and git("--git-dir", str(bench.world.public), "rev-parse", f"{candidate}^{{tree}}")
          == git("--git-dir", str(bench.world.cache), "rev-parse", f"{candidate}^{{tree}}"),
          f"{t}: what landed is the approved commit, its tree and its parent")
    facts = attempts(bench.board, t)[-1]["facts"]
    check(facts["branch"] == "main" and facts["public_url"] == str(bench.world.public)
          and facts["push_url"] == str(bench.world.public), f"{t}: published where the board's cache points: {facts}")


class Crash(Exception):
    """The Publisher's process stopping, at a chosen command."""


def crashes(bench: Bench) -> None:
    """A run that stops anywhere is resumed from facts: never a second push, never a second record."""
    world, board = bench.world, bench.board

    def pushing(argv):
        return "push" in argv

    def refreshing(argv):
        return ("fetch" in argv and str(world.cache) in argv
                and "+refs/heads/main:refs/remotes/origin/main" in argv and world.main() != world.cache_main())

    def before(which):
        def answer(argv):
            if which(argv):
                raise Crash(argv[3])
            return None
        return answer

    def after(which):
        def hook(argv, _result):
            if which(argv):
                raise Crash(argv[3])
        return hook

    # (how it stops, the journal step it stops at, whether the push had happened, whether the bundle is lost)
    for runner, step, landed, lost_bundle in ((Runner(after=after(pushing)), "pushing", True, False),
                                              (Runner(before(pushing)), "pushing", False, True),
                                              (Runner(before(refreshing)), "pushed", True, False)):
        t, candidate = bench.ticket()
        try:
            bench.publish(t, runner)
            check(False, "the crash stops the run")
        except Crash:
            pass
        journal = attempts(board, t)
        check([(a["state"], a["step"]) for a in journal] == [("open", step)] and row(board, t)["state"] == "publication"
              and comments(board, t) == [] and (world.main() == candidate) == landed,
              f"{t}: stopped at {step}, nothing recorded, public main {'carries' if landed else 'lacks'} it: {journal}")
        if lost_bundle:
            Path(journal[0]["facts"]["bundle"]).unlink()
        again = Runner()
        result = bench.publish(t, again)
        said = comments(board, t)
        check(result["outcome"] == "published" and world.main() == candidate and len(said) == 1
              and f"from journal step {step}" in said[0] and "resumed attempt" in said[0],
              f"{t}: resumed and recorded once: {said}")
        check(again.count("push") == (0 if landed else 1) and ("already on public main" in said[0]) == landed,
              f"{t}: pushed again only when the first push never happened: {again.count('push')}, {said}")
        check(not lost_bundle or "rebuilt the bundle" in said[0], f"{t}: a lost bundle is rebuilt: {said}")
        check([a["state"] for a in attempts(board, t)] == ["published"], f"{t}: one attempt, published")


def race(bench: Bench) -> None:
    """Main moves between the Publisher's read and its push: the lease refuses, and the ticket goes back."""
    world = bench.world
    t, candidate = bench.ticket()
    theirs = world.commit([world.main()], {"src/race.py": "r\n"}, "landed during the push")

    def land_first(argv):
        if "push" in argv:
            world.land_on_main(theirs)
        return None

    bench.notices()
    result = bench.publish(t, Runner(land_first))
    said = comments(bench.board, t)
    check(result["outcome"] == "returned" and world.main() == theirs
          and f"moved to {theirs[:12]} before {candidate[:12]} could be published" in said[0],
          f"the compare-and-swap refused, nothing overwritten, the ticket returned: {result} {said}")
    # Main rewound to an ancestor while the push was in flight: a plain push would fast-forward from there and
    # publish over a tip nobody approved; the lease names the tip that was checked, and refuses.
    t, candidate = bench.ticket()
    older = git("--git-dir", str(world.public), "rev-parse", "main^")

    def rewind_first(argv):
        if "push" in argv:
            git("--git-dir", str(world.public), "update-ref", "refs/heads/main", older)
        return None

    bench.notices()
    result = bench.publish(t, Runner(rewind_first))
    said = comments(bench.board, t)
    check(result["outcome"] == "returned" and world.main() == older
          and f"moved to {older[:12]} before {candidate[:12]} could be published" in said[0],
          f"a rewound main is not fast-forwarded over: the lease refused it: {result} {said}")


def gates(bench: Bench) -> None:
    """Never a waived gate: missing sign-offs and unresolved size findings are the Director's; held work waits."""
    board, world = bench.board, bench.world
    t, candidate = bench.ticket()
    board.sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET audit_signoff = false "
              f"WHERE id = '{t}';")
    escalated(bench, t, "gate", None, "missing sign-off audit_signoff", "does not waive a gate")
    check(world.main() != candidate, "nothing was published without its sign-off")
    # Size review on, with an open finding against the candidate.
    t, candidate = bench.ticket()
    board.sql("INSERT INTO ticket_board.size_review_policy (enabled_by, baseline_commit, inventory) "
              f"VALUES ('director', '{world.base}', '[]');")
    board.sql(f"INSERT INTO ticket_board.size_scans (ticket_id, candidate, base) VALUES ('{t}', '{candidate}', "
              f"'{world.main()}') ON CONFLICT (ticket_id) DO UPDATE SET candidate = EXCLUDED.candidate, "
              "base = EXCLUDED.base, error = NULL;")
    board.sql("INSERT INTO ticket_board.size_findings (ticket_id, path, reason, state, candidate, before_lines, "
              f"after_lines) VALUES ('{t}', 'src/big.py', 'growth', 'open', '{candidate}', 1300, 1400);")
    escalated(bench, t, "gate", None, "unresolved size findings", "src/big.py (growth)")
    board.sql(f"UPDATE ticket_board.size_findings SET state = 'resolved' WHERE ticket_id = '{t}';")
    retry(bench, t, candidate)
    board.sql("DELETE FROM ticket_board.size_review_policy;")
    # Blocked or held work waits where it is: no attempt, no move, no message.
    t, candidate = bench.ticket()
    blocked = board.act(t, "set_blockers", "director", blocked_by=["operator:release_manager"], blocked_reason="wait")
    check(short(blocked) == "ok", f"the Director blocks a ticket in publication: {short(blocked)}")
    blocked_run = bench.attempt(t)
    check(blocked_run["outcome"] == "not_ready" and attempts(board, t) == [] and pending(board) == []
          and world.main() != candidate, f"a blocked ticket is not the Publisher's to work on: {blocked_run}")
    released = board.act(t, "release_external_blocker", "director", ref="operator:release_manager", reason="ready")
    check(short(released) == "ok", f"unblocked: {short(released)}")
    check(short(board.act(t, "set_manually_controlled", "director", manually_controlled=True)) == "ok", "held")
    held_run = bench.attempt(t)
    check(held_run["outcome"] == "not_ready" and attempts(board, t) == [], f"a held ticket waits too: {held_run}")
    check(short(board.act(t, "set_manually_controlled", "director", manually_controlled=False)) == "ok", "released")
    waiting = pending(board)
    result = bench.publish(t)
    check(waiting == [t] and result["outcome"] == "published" and world.main() == candidate,
          f"released and unblocked, it is published: {waiting} {result} {row(board, t)}")


def pending(board: Board) -> list[str]:
    with psycopg.connect(board.listener_url, autocommit=True) as conn:
        return publisher.Journal(conn).pending()


def no_code(bench: Bench) -> None:
    """A ticket submitted without code passes publication without a push or any git at all."""
    board = bench.board
    bench.settle()
    bench.number += 1
    t = f"PGU-{bench.number}"
    bench.made.append(t)
    fixture.seed_postgres_ticket(board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director")
    check(short(board.act(t, "route", "director", state="in_progress", assignee="app")) == "ok", "routed")
    check(short(board.act(t, "submit_to_audit_without_commit", "app", reason="a finding, no code")) == "ok", "no code")
    check(short(board.act(t, "audit_sign_off", "audit")) == "ok", "Audit signs off")
    main = bench.world.main()
    runner = Runner()
    result = bench.publish(t, runner)
    check(result["outcome"] == "published" and runner.seen == [] and bench.world.main() == main
          and row(board, t)["state"] == "director_review"
          and comments(board, t)[0].startswith("No commit to publish"),
          f"nothing to publish, said so, and on to Final Sign-Off: {result}")


def guard(bench: Bench) -> None:
    """Only the Publisher takes its moves, only through publisher_finish; each attempt hands off once."""
    board = bench.board
    t, candidate = bench.ticket()
    before = board.snapshot(t)
    http = {who: board.act(t, action, who, reason="x", commit_hash=candidate)
            for who in ("publisher", "director", "app")
            for action in ("publication_published",)}
    http["publisher_return"] = board.act(t, "publication_return", "publisher", reason="x")
    http["publisher_escalate"] = board.act(t, "publication_escalate", "publisher", reason="x")
    guarded = "only the Publisher takes publication moves"
    check(all(guarded in http[key].get("refused", "") for key in ("publisher", "publisher_return", "publisher_escalate"))
          and all(f"{who} cannot call publication_published" in http[who].get("refused", "") for who in ("director", "app")),
          f"no HTTP caller takes a Publisher move -- not even one naming itself publisher: {http}")
    sql = {
        "as_publisher": board.as_role_sql("publisher", f"SELECT ticket_board.perform_workflow_action('{t}', "
                                                       "'publication_published', '{\"text\": \"x\"}'::jsonb);"),
        "recover": board.as_role_sql("director", f"SELECT ticket_board.recover_stalled_ticket('{t}', 'x');"),
        "finish_as_service": board.as_role_sql("director", "SELECT ticket_board.publisher_finish(1, 'published', "
                                                           "'x', '{}'::jsonb, '[]'::jsonb);"),
        "begin_as_service": board.as_role_sql("director", f"SELECT ticket_board.publisher_begin('{t}');"),
    }
    check("permission denied for function publisher_finish" in sql["finish_as_service"].get("refused", "")
          and "permission denied for function publisher_begin" in sql["begin_as_service"].get("refused", "")
          and "refused" in sql["recover"] and "does not exist" not in sql["recover"]["refused"],
          f"nor does any SQL caller but the listener, and a stalled-ticket recovery cannot take one: {sql}")
    check(board.snapshot(t) == before, f"and nothing about the ticket moved; the recovery said: {sql['recover']}")
    check("only the Publisher takes publication moves" in sql["as_publisher"]["refused"],
          f"the service naming itself publisher meets the guard: {sql['as_publisher']}")
    # Verified-only, gated-only, exact-candidate-only, at the database, whatever the Publisher says.
    with psycopg.connect(board.listener_url, autocommit=True) as conn:
        journal = publisher.Journal(conn)
        info = journal.begin(t)
        attempt = int(info["attempt"])
        unverified = refusal(lambda: journal.finish(attempt, "published", "x", {"candidate_full": candidate}, []))
        verified = refusal(lambda: journal.step(attempt, "verified", "", {}, []))
        wrong = refusal(lambda: journal.finish(attempt, "published", "x", {"candidate_full": "f" * 40}, []))
        backwards = refusal(lambda: journal.step(attempt, "started", "", {}, []))
        first = answer(lambda: journal.finish(attempt, "published", "Published (by hand, in the test).",
                                              {"candidate_full": candidate}, []))
        second = answer(lambda: journal.finish(attempt, "returned", "and returned too?", {}, []))
        third = answer(lambda: journal.finish(attempt, "escalated", "and escalated too?", {}, []))
    check(verified == "accepted", f"the journal goes forward: {verified}")
    check("is not verified" in unverified and "is not PGU-" in wrong and "recorded candidate" in wrong
          and "cannot follow" in backwards, f"the database refuses unverified, foreign and backwards records: "
                                            f"{unverified} | {wrong} | {backwards}")
    check(first.get("recorded") is True and second.get("recorded") is False and third.get("recorded") is False
          and row(board, t)["state"] == "director_review" and len(comments(board, t)) == 1,
          f"an attempt hands its ticket on once: {first} {second} {third}")
    # An ungated review is always owed: the rule asked directly, of this board's own document with Audit ungated.
    owed = board.sql("SELECT array_to_string(ticket_board.publication_missing_signoffs(jsonb_set(cfg, '{stages}', "
                     "(SELECT jsonb_agg(CASE WHEN s->>'name' = 'audit' THEN s || '{\"gate\": null, \"skip_to\": null}' "
                     "ELSE s END) FROM jsonb_array_elements(cfg->'stages') s)), '{\"audit_signoff\": false, "
                     "\"needs_audit\": false}'::jsonb), ',') FROM (SELECT ticket_board.declared_workflow() AS cfg) d;")
    check(owed == "audit_signoff", f"an ungated review's sign-off is owed whatever its gate flag says: {owed!r}")
    # A sign-off missing at the moment of recording is refused by the database itself, verified journal or not.
    t2, candidate2 = bench.ticket()
    with psycopg.connect(board.listener_url, autocommit=True) as conn:
        journal2 = publisher.Journal(conn)
        info2 = journal2.begin(t2)
        journal2.step(int(info2["attempt"]), "verified", "", {"candidate_full": candidate2}, [])
        board.sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET audit_signoff = false "
                  f"WHERE id = '{t2}';")
        unsigned = refusal(lambda: journal2.finish(int(info2["attempt"]), "published", "x",
                                                   {"candidate_full": candidate2}, []))
    check("is missing sign-off audit_signoff" in unsigned and row(board, t2)["state"] == "publication",
          f"the database refuses to record a publication that owes a sign-off: {unsigned}")


def answer(call) -> dict:
    """What the call returned, or what it raised, as an answer."""
    try:
        return call() or {}
    except Exception as exc:  # noqa: BLE001 - an answer
        return {"raised": str(exc).splitlines()[0]}


def refusal(call) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - a refusal is an answer
        return str(exc).splitlines()[0]
    return "accepted"


def superseded(bench: Bench) -> None:
    """The Director takes the ticket away mid-run: the attempt is superseded, and a publication is still said."""
    board, world = bench.board, bench.world
    t, candidate = bench.ticket()

    def defer_then_push(argv):
        if "push" in argv:
            check(short(board.act(t, "defer", "director")) == "ok", "the Director defers the ticket mid-push")
        return None

    result = bench.attempt(t, Runner(defer_then_push))
    said = comments(board, t)
    check(result.get("recorded") is False and row(board, t)["state"] == "backlog" and world.main() == candidate
          and len(said) == 1 and said[0].startswith("The ticket left publication before this could be recorded")
          and [a["state"] for a in attempts(board, t)] == ["superseded"],
          f"nothing moved, the attempt superseded, the publication still on the record: {result} {said}")


def listener_pass(bench: Bench) -> None:
    """The listener starts the worker, which publishes, and starts nothing when nothing waits."""
    board, world = bench.board, bench.world
    t, candidate = bench.ticket()
    listener = board.listener
    listener.publisher_repositories = lambda: [str(world.cache)]
    listener.publisher_root = world.state
    with psycopg.connect(board.listener_url, autocommit=True) as conn:
        listener.refresh_workflow(conn)
        started = publisher_pass.run(listener, conn)
        if getattr(listener, "publisher_worker", None) is not None:
            listener.publisher_worker.join(120)
        again = publisher_pass.run(listener, conn)
    check(started == 1 and again == 0 and row(board, t)["state"] == "director_review" and world.main() == candidate,
          f"the listener's pass published {t} in the background, then had nothing to do: {started} {again}")
    # And inside the listener's own loop, as the unit runs it.
    t, candidate = bench.ticket()
    board.busy.clear()
    listener.listen_once(max_notifications=listener.delivered_count + 20)
    if getattr(listener, "publisher_worker", None) is not None:
        listener.publisher_worker.join(120)
    board.busy.clear()
    listener.listen_once(max_notifications=listener.delivered_count + 20)
    check(row(board, t)["state"] == "director_review" and world.main() == candidate
          and any(target == "pgu-director:0.0" and t in message for target, message in board.sent[-3:]),
          f"the real loop starts the Publisher, and its next pass tells the Director: {board.sent[-3:]}")


def without(cluster, root: Path) -> None:
    """A board without the Publisher behaves exactly as before, and the listener pass does nothing there."""
    bench = Bench(cluster, "plain", root, example())
    try:
        t, candidate = bench.ticket()
        check(row(bench.board, t)["state"] == "director_review" and bench.world.main() != candidate,
              "Audit's approval goes on to Final Sign-Off as before, and nothing is published")
        with psycopg.connect(bench.board.listener_url, autocommit=True) as conn:
            bench.board.listener.refresh_workflow(conn)
            asked = []

            class Recording:
                def execute(self, statement, *args):
                    asked.append(statement)
                    return conn.execute(statement, *args)

            check(publisher_pass.run(bench.board.listener, Recording()) == 0 and asked == [] and pending(bench.board) == []
                  and getattr(bench.board.listener, "publisher_worker", None) is None,
                  "the listener's pass starts nothing on a board without the Publisher")
    finally:
        bench.close()


def upgraded(cluster, root: Path) -> None:
    """Main's board, upgraded by this tree's migrations, publishes like a fresh one."""
    before = root / "before"
    before.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE, "scripts"], check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(before)], input=archive.stdout, check=True)
    check(not (before / "scripts/ticket_board/migrations/pgu1004_syrd625_publisher.sql").exists(),
          "the before-tree has no Publisher")
    bench = Bench(cluster, "up", root, before=before)
    try:
        ordinary(bench)
    finally:
        bench.close()


def scenario() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd625-") as tmp, temporary_cluster(prefix="syrd625-pg-") as cluster:
        root = Path(tmp)
        bench = Bench(cluster, "pub", root)
        try:
            for case in (ordinary, toward_user_review, lineage, director_failures, repairs, crashes, race, gates,
                         no_code, guard, superseded, listener_pass):
                if chosen(case.__name__):
                    case(bench)
        finally:
            bench.close()
        if chosen("without"):
            without(cluster, root)
        if chosen("upgraded"):
            upgraded(cluster, root)


def chosen(name: str) -> bool:
    """Every case, or those SYRD625_CASES names (comma-separated): for a quick rerun, never for the record."""
    only = os.environ.get("SYRD625_CASES", "").strip()
    return not only or name in only.split(",")


def main() -> int:
    scenario()
    print(f"publisher_board_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
