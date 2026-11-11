#!/usr/bin/env python3
"""SYRD-754: a public main the Publisher did not move is the Director's -- never published over, cached or recorded.

Real boards and real git, on publisher_board_test's fixtures (its forge, trusted
cache, implementer clone and Bench). A role's push in the fixture is
`World.land_on_main`; the one legitimate way main moves is `Bench.integrate`,
another approved ticket the Publisher published.

Cases:
- `provenance`: the observed already-public sequence and the Director's answer
  (for that exact tip only); the candidate itself already on main; a refused
  push's candidate later put on main; a rewind through an already-published
  candidate's record; two journalled pushes leased on one tip; HEAD on a
  candidate ref; main as a symbolic ref.
- `cache_race`: main moved between the proof of its tip and the trusted cache's
  fetch, before the read-back and before the post-push refresh; a board refresh
  that ran before the Publisher's mark (put back on the record) and one that
  starts while the Publisher holds the cache's lock (it waits and leaves the
  cache alone); the board's refresh-on-miss leaving the marked branch alone.
- `hosted`, on a `file://` remote: a roles/ HEAD and a landed candidate before
  any record, a first publication resumed after its own push, a violation with
  the cache untouched, and a changed publication target answered by the Director.

SYRD625_CASES picks cases, as in publisher_board_test.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import publisher_board_test as pb  # noqa: E402
from publisher_board_test import (Bench, Crash, Runner, attempts, check, chosen, comments, escalated,  # noqa: E402
                                  failed, fixture, git, retry, short, temporary_cluster)
from scripts.ticket_board import commit_cache  # noqa: E402


def provenance(bench: Bench) -> None:
    """SYRD-754: a public main the Publisher did not make is the Director's, never published over or cached."""
    board, world = bench.board, bench.world
    bench.integrate({"src/provenance-base.py": "b\n"})  # a recorded publication to start from
    # The observed sequence: a candidate reaches public main before Audit; the corrective commit on it is approved.
    reviewed = world.main()
    early = world.commit([reviewed], {"src/retarget.py": "v1\n"}, "retarget, before any review")
    world.publish("roles/ops/early", early)
    world.land_on_main(early)
    t, corrective = bench.ticket(parents=[early], files={"src/retarget.py": "v2\n"})
    said = escalated(bench, t, "provenance", None, f"reads {early[:12]}", f"left it at {reviewed[:12]}",
                     "1 commit(s) on top of it that no Publisher attempt pushed", "retarget, before any review",
                     "did not bring the trusted cache", "recorded nothing as published", "The Director decides")
    check(world.main() == early and world.cache_main() == reviewed,
          f"main is left as it was found, and the trusted cache is not repaired to the unreviewed tip: {said}")
    check(attempts(board, t)[-1]["facts"]["unrecorded_tip"] == early, "the escalation names the tip it refused")
    # The Director answers by retrying with that tip in place: this ticket takes exactly it as its base.
    check(short(board.act(t, "publication_retry", "director")) == "ok", "the Director retries")
    result = bench.publish(t)
    said = comments(board, t)[-1]
    check(result["outcome"] == "published" and world.main() == corrective and world.cache_main() == corrective
          and f"Pushed as a fast-forward from {early[:12]}" in said
          and "the ticket came back only through the Director" in said,
          f"published on the tip the Director accepted, and the evidence says whose decision it was: {said}")
    # The answer is for that tip only: main moved again, and the same ticket's next round is refused again.
    t, candidate = bench.ticket()
    unreviewed = world.commit([world.main()], {"src/u.py": "u\n"}, "unreviewed")
    world.land_on_main(unreviewed)
    escalated(bench, t, "provenance", None, f"reads {unreviewed[:12]}")
    later = world.commit([unreviewed], {"src/v.py": "v\n"}, "unreviewed again")
    world.land_on_main(later)
    check(short(board.act(t, "publication_retry", "director")) == "ok", "retried while main moved on")
    escalated(bench, t, "provenance", None, f"reads {later[:12]}", "2 commit(s) on top of it")
    # Restored to the record, the retry publishes the ordinary way.
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", corrective)
    retry(bench, t, candidate)
    # The candidate itself put on main before review: never "already published".
    t, candidate = bench.ticket()
    before = world.main()
    world.land_on_main(candidate)
    escalated(bench, t, "provenance", None, f"reads {candidate[:12]}", "no Publisher attempt pushed", moved=True)
    check(not any("already on public main" in text for text in comments(board, t)),
          "a candidate that reached main without the Publisher is not recorded as published")
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", before)
    retry(bench, t, candidate)
    check(git("--git-dir", str(world.public), "rev-parse", f"{candidate}^") == before,
          "after main is restored, the retry publishes it by the Publisher's own push")
    # A push the Publisher journalled and git refused made nothing: that candidate put on main by a role is refused.
    t, candidate = bench.ticket()
    before = world.main()
    refusing = Runner(lambda argv: failed(argv, "fatal: Could not read from remote repository.\n"
                                                "git@x: Permission denied (publickey).") if "push" in argv else None)
    escalated(bench, t, "credential", refusing, "Permission denied")
    world.land_on_main(candidate)
    check(short(board.act(t, "publication_retry", "director")) == "ok", "retried")
    escalated(bench, t, "provenance", None, f"reads {candidate[:12]}", moved=True)
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", before)
    retry(bench, t, candidate)
    # A ticket whose candidate was published already is recorded without a push -- and that record is no path
    # back: main rewound onto the old candidate afterwards is still a move nobody published.
    older = git("--git-dir", str(world.public), "rev-parse", "main^")
    newest = world.main()
    t, candidate = bench.ticket(commit=older)
    result = bench.publish(t)
    check(result["outcome"] == "published" and "It was already on public main" in comments(board, t)[-1]
          and world.main() == newest, f"an already-published candidate is recorded, nothing pushed: {result}")
    t, candidate = bench.ticket()
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", older)
    escalated(bench, t, "provenance", None, f"reads {older[:12]}", "main was moved back", moved=True)
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", newest)
    retry(bench, t, candidate)
    # Two journalled pushes leased on one tip: one stopped before it pushed, one pushed and was superseded before
    # it could verify. The tip the second made is the Publisher's own: the next ticket publishes on it.
    t, stopped = bench.ticket()

    def stop_before_push(argv):
        if "push" in argv:
            raise Crash("push")
        return None

    try:
        bench.publish(t, Runner(stop_before_push))
        check(False, "the crash stops the run")
    except Crash:
        pass
    check([(a["state"], a["step"]) for a in attempts(board, t)] == [("open", "pushing")] and world.main() != stopped,
          "the first stopped at its journalled push, having pushed nothing")
    t, pushed = bench.ticket()

    def defer_mid_push(argv):
        if "push" in argv:
            board.act(t, "defer", "director")
        return None

    bench.attempt(t, Runner(defer_mid_push))
    check(world.main() == pushed and [a["state"] for a in attempts(board, t)] == ["superseded"],
          "the second pushed, and was superseded before it verified")
    t, candidate = bench.ticket()
    result = bench.publish(t)
    check(result["outcome"] == "published" and world.main() == candidate,
          f"the walk takes the journalled push that leads to the tip, not the one that stopped: {result}")
    # Aliases: HEAD moved onto a candidate ref, and main made a symbolic ref to one.
    t, candidate = bench.ticket()
    git("--git-dir", str(world.public), "symbolic-ref", "HEAD", f"refs/heads/roles/app/{t.lower()}")
    escalated(bench, t, "authority", None, f"HEAD names roles/app/{t.lower()}", "publishes to main there",
              "does not change its integration branch")
    git("--git-dir", str(world.public), "symbolic-ref", "HEAD", "refs/heads/main")
    tip = world.main()
    git("--git-dir", str(world.public), "update-ref", f"refs/heads/roles/app/{t.lower()}-alias", tip)
    git("--git-dir", str(world.public), "symbolic-ref", "refs/heads/main", f"refs/heads/roles/app/{t.lower()}-alias")
    check(short(board.act(t, "publication_retry", "director")) == "ok", "retried")
    escalated(bench, t, "authority", None, "refs/heads/main of", "is a symbolic ref to", "would move both")
    check(git("--git-dir", str(world.public), "rev-parse", f"refs/heads/roles/app/{t.lower()}-alias") == tip,
          "nothing was pushed through the alias")
    git("--git-dir", str(world.public), "update-ref", "--no-deref", "refs/heads/main", tip)
    git("--git-dir", str(world.public), "update-ref", "-d", f"refs/heads/roles/app/{t.lower()}-alias")
    retry(bench, t, candidate)


def cache_race(bench: Bench) -> None:
    """Main moved between the proof of its tip and the trusted cache's fetch: the cache is not brought to it."""
    board, world = bench.board, bench.world

    def land_at(which, commit):
        def answer(argv):
            if which(argv) and world.main() != commit:
                world.land_on_main(commit)
            return None
        return answer

    def cache_fetch(argv):
        """Any fetch of public main into the trusted cache, whatever ref it names there."""
        return ("fetch" in argv and str(world.cache) in argv
                and any(part.startswith("+refs/heads/main:") for part in argv))

    def no_unverified_repair(t: str, commit: str) -> None:
        check(not any(f"to {commit[:12]}" in text and "refreshed the trusted cache" in text
                      for text in comments(board, t)), f"{t}: no repair claims the unverified {commit[:12]}")
    bench.integrate({"src/cache-race-base.py": "b\n"})  # a recorded publication, with a parent, to start from
    # Before the push: a stale cache must be refreshed, and main moves just before that fetch.
    t, candidate = bench.ticket()
    proven = world.main()
    stale = git("--git-dir", str(world.public), "rev-parse", "main^")
    git("--git-dir", str(world.cache), "update-ref", "refs/remotes/origin/main", stale)
    sneaked = world.commit([proven], {"src/sneak-cache.py": "s\n"}, "landed during the cache refresh")
    runner = Runner(land_at(cache_fetch, sneaked))
    escalated(bench, t, "provenance", runner, f"reads {sneaked[:12]}", moved=True)
    check(world.cache_main() == stale and runner.count("push") == 0,
          f"the trusted cache was left where it was, and nothing was pushed: {world.cache_main()[:12]}")
    no_unverified_repair(t, sneaked)
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", proven)
    retry(bench, t, candidate)
    # After the push: main moved past the candidate before the read-back, or before the cache's fetch.
    for when in ("read-back", "cache"):
        t, candidate = bench.ticket()
        after = world.commit([candidate], {f"src/after-{when}.py": "a\n"}, f"landed after the push, at the {when}")

        def at(argv, when=when, candidate=candidate):
            if world.main() != candidate:
                return False  # not pushed yet
            if when == "cache":
                return cache_fetch(argv)
            return "fetch" in argv and "/work.git" in " ".join(argv)

        before_cache = world.cache_main()
        escalated(bench, t, "provenance", Runner(land_at(at, after)), f"reads {after[:12]}", moved=True)
        check(world.cache_main() == before_cache and world.main() == after,
              f"{when}: the cache is not brought to a tip past the candidate that nobody published")
        no_unverified_repair(t, after)
        git("--git-dir", str(world.public), "update-ref", "refs/heads/main", candidate)
        check(short(board.act(t, "publication_retry", "director")) == "ok", "retried")
        result = bench.publish(t)
        check(result["outcome"] == "published" and world.cache_main() == candidate
              and "It was already on public main" in comments(board, t)[-1],
              f"{when}: main restored to the Publisher's own push, the retry records it: {result}")
    # A board refresh that ran before this attempt marked the cache (an unmarked cache, or the first attempt):
    # it moved the cache's main to an unreviewed tip; the escalation puts the cache back on the record.
    t, candidate = bench.ticket()
    proven = world.main()
    subprocess.run(["git", "--git-dir", str(world.cache), "symbolic-ref", "-d", "refs/publisher/integration"],
                   capture_output=True)  # unmarked, as an existing cache is before its first marked attempt
    early = world.commit([proven], {"src/early-refresh.py": "e\n"}, "pushed by a role before the mark")
    ran = []

    def refresh_before_mark(argv, _result):
        if "ls-remote" in argv and "refs/heads/main" in argv and not ran:
            world.land_on_main(early)
            ran.append(commit_cache.refresh_commit_repos((world.cache,)))

    said = escalated(bench, t, "provenance", Runner(after=refresh_before_mark), f"reads {early[:12]}",
                     f"read {early[:12]}, which nobody published; it was put back to {proven[:12]}", moved=True)
    check(ran == [True] and world.cache_main() == proven,
          f"the early refresh moved the cache, and the escalation put it back on the record: {ran} {said}")
    no_unverified_repair(t, early)
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", proven)
    retry(bench, t, candidate)
    # A board refresh that starts while the Publisher holds the cache's lock -- at the moment it writes its
    # mark -- waits, and leaves the cache alone this time.
    t, candidate = bench.ticket()
    proven = world.main()
    late = world.commit([proven], {"src/late-refresh.py": "l\n"}, "pushed by a role during the mark")
    ran.clear()
    wait, commit_cache.LOCK_WAIT_SECONDS = commit_cache.LOCK_WAIT_SECONDS, 0.3

    def refresh_at_mark(argv):
        if "symbolic-ref" in argv and "refs/publisher/integration" in argv and not ran:
            world.land_on_main(late)
            ran.append(commit_cache.refresh_commit_repos((world.cache,)))
        return None

    try:
        escalated(bench, t, "provenance", Runner(refresh_at_mark), f"reads {late[:12]}", moved=True)
    finally:
        commit_cache.LOCK_WAIT_SECONDS = wait
    check(ran == [False] and world.cache_main() == proven,
          f"the refresh found the cache locked and left it at the proven tip: {ran} {world.cache_main()[:12]}")
    no_unverified_repair(t, late)
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", proven)
    retry(bench, t, candidate)
    # The board's own refresh, on a commit it has not seen, leaves the branch the Publisher marked alone.
    proven = world.main()
    marked = subprocess.run(["git", "--git-dir", str(world.cache), "symbolic-ref", "-q", "refs/publisher/integration"],
                            capture_output=True, text=True).stdout.strip()
    check(marked == "refs/remotes/origin/main", f"the Publisher marked the tracking ref only it moves: {marked!r}")
    unreviewed = world.commit([proven], {"src/unreviewed-refresh.py": "u\n"}, "pushed by a role")
    world.land_on_main(unreviewed)
    bench.settle()
    bench.number += 1
    t = f"PGU-{bench.number}"
    bench.made.append(t)
    fixture.seed_postgres_ticket(board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director")
    check(short(board.act(t, "route", "director", state="in_progress", assignee="app")) == "ok", f"{t} routed")
    unseen = world.commit([proven], {f"src/{t.lower()}.py": "x\n"}, f"{t}: published, not yet in the cache")
    git("push", "-q", str(world.public), f"{unseen}:refs/heads/roles/app/{t.lower()}", cwd=world.dev)
    answer = board.act(t, "submit_to_audit", "app", commit_hash=unseen)
    check(short(answer) == "ok" and git("--git-dir", str(world.cache), "rev-parse",
                                        f"refs/remotes/origin/roles/app/{t.lower()}") == unseen,
          f"the board's refresh-on-miss still brings a published candidate in: {short(answer)}")
    check(world.cache_main() == proven, f"and leaves the cache's main at the proven {proven[:12]}, not the "
                                        f"unreviewed {unreviewed[:12]}: {world.cache_main()[:12]}")
    git("--git-dir", str(world.public), "update-ref", "refs/heads/main", proven)


def hosted(cluster, root: Path) -> None:
    """The same rules on a hosted-style remote (`file://`): the first publication starts the record; then a main
    moved outside the Publisher is escalated, with the cache untouched."""
    bench = Bench(cluster, "hosted", root)
    try:
        world = bench.world
        url = f"file://{world.public}"
        git("--git-dir", str(world.cache), "config", "remote.origin.url", url)
        # No publication on record yet: a HEAD naming a role's candidate ref is no integration branch...
        t, candidate = bench.ticket()
        git("--git-dir", str(world.public), "symbolic-ref", "HEAD", f"refs/heads/roles/app/{t.lower()}")
        escalated(bench, t, "authority", None, f"HEAD names roles/app/{t.lower()}", "a role's candidate namespace")
        git("--git-dir", str(world.public), "symbolic-ref", "HEAD", "refs/heads/main")
        # ...and a candidate already on main is not a starting point but a violation.
        world.land_on_main(candidate)
        check(short(bench.board.act(t, "publication_retry", "director")) == "ok", "retried")
        escalated(bench, t, "provenance", None, "no publication to main is recorded on this board",
                  f"already carries {candidate[:12]}, which the Publisher never pushed")
        git("--git-dir", str(world.public), "update-ref", "refs/heads/main", world.base)
        check(short(bench.board.act(t, "publication_kick_back", "director", reason="rebuild it")) == "ok",
              "the Director returns it")
        # The first publication stops right after its push: resumed, its own journalled push made the tip.
        t, candidate = bench.ticket()

        def stop_after_push(argv, _result):
            if "push" in argv:
                raise Crash("push")

        try:
            bench.publish(t, Runner(after=stop_after_push))
            check(False, "the crash stops the run")
        except Crash:
            pass
        check(world.main() == candidate, "the first push landed before the stop")
        result = bench.publish(t)
        said = comments(bench.board, t)[-1]
        check(result["outcome"] == "published" and world.main() == candidate and f"main of {url}" in said
              and "is where the Publisher's record starts" in said and "resumed attempt" in said,
              f"first publication over file://, resumed after its own push: {said}")
        t, candidate = bench.ticket()
        theirs = world.commit([world.main()], {"src/theirs.py": "t\n"}, "pushed by a role")
        world.land_on_main(theirs)
        escalated(bench, t, "provenance", None, f"public main of {url} reads {theirs[:12]}")
        check(world.cache_main() != theirs, "the trusted cache is not brought to it")
        # A trusted cache pointed somewhere new is the Director's once; answered, the new target's record starts.
        t, candidate = bench.ticket(parents=[theirs])
        git("--git-dir", str(world.cache), "config", "remote.origin.url", str(world.public))
        escalated(bench, t, "authority", None, f"origin is now {world.public}", f"publishes to {url}")
        check(short(bench.board.act(t, "publication_retry", "director")) == "ok",
              "the Director makes the new target the target")
        result = bench.publish(t)
        said = comments(bench.board, t)[-1]
        check(result["outcome"] == "published" and "is where the Publisher's record starts" in said
              and world.main() == candidate,
              f"published to the new target, its record started by the answer: {said}")
    finally:
        bench.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd754-") as tmp, temporary_cluster(prefix="syrd754-pg-") as cluster:
        root = Path(tmp)
        bench = Bench(cluster, "prov", root)
        try:
            for case in (provenance, cache_race):
                if chosen(case.__name__):
                    case(bench)
        finally:
            bench.close()
        if chosen("hosted"):
            hosted(cluster, root)
    print(f"publisher_provenance_test: ok ({pb.CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
