#!/usr/bin/env python3
"""SYRD-848: the Publisher publishes the board-recorded implementer + documentation pair as it stands.

A Librarian's documentation sign-off makes the candidate two commits on the tip -- the implementer's, then the
Librarian's -- and the Publisher, which publishes one, returned every one for a squash. It now accepts exactly the
pair the board recorded (ticket_audit_returns, read by publication_documentation_pair), when the implementer's commit
is one commit on the public tip, and pushes both in one leased fast-forward, each commit keeping its author.

On publisher_board_test's real world -- a bare forge whose HEAD is main, the board's trusted cache cloned from it, a
production-built board with the documentation gate (a Librarian) and the Publisher, the real listener functions:

* the recorded pair publishes: both SHAs on main, in order, each with its own author, the evidence naming both;
* after Audit's documentation-only return, a Librarian's correction (a second documentation commit) publishes all
  three commits, and a Librarian's no change publishes the pair;
* the implementer's own rework clears the recorded layer, and the Librarian's next commit starts it again;
* an unrecorded two-commit candidate is returned for lineage, as before;
* a recorded pair whose implementer commit is no longer on the tip is returned;
* the push repository proves the pair again from the bundle, and escalates (pushing nothing) when it does not hold;
* a ticket the Librarian approved with no change publishes its one commit, as before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402
import publisher_board_test as pb  # noqa: E402
from publisher_board_test import Bench, comments, example, fixture, git, row, short, temporary_cluster  # noqa: E402
from scripts.ticket_board.documentation_gate import candidate_ref, with_documentation_gate  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def authored(world, parent: str, files: dict[str, str], message: str, who: str) -> str:
    """A commit on `parent` by `who` (its author and committer), so each commit's author can be told apart."""
    index = world.root / f"{who}.index"
    env = {**os.environ, **pb.GIT_ENV, "GIT_INDEX_FILE": str(index), "GIT_AUTHOR_NAME": who,
           "GIT_AUTHOR_EMAIL": f"{who}@example.invalid", "GIT_COMMITTER_NAME": who,
           "GIT_COMMITTER_EMAIL": f"{who}@example.invalid"}

    def run(*args: str, stdin: str | None = None) -> str:
        return subprocess.run(["git", "-C", str(world.dev), *args], input=stdin, capture_output=True, text=True,
                              env=env, check=True).stdout.strip()

    run("read-tree", parent)
    for name, text in files.items():
        blob = run("hash-object", "-w", "--stdin", stdin=text)
        run("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    return run("commit-tree", run("write-tree"), "-p", parent, "-m", message)


def author(world, commit: str) -> str:
    return git("--git-dir", str(world.public), "log", "-1", "--format=%an <%ae>", commit)


class Pair:
    """Tickets through Implementation and Documentation on the bench, ready for the Publisher."""

    def __init__(self, bench: Bench) -> None:
        self.bench, self.board, self.world = bench, bench.board, bench.world

    def submitted(self, *, documentation: bool = True, parents: list[str] | None = None) -> tuple[str, str]:
        bench = self.bench
        bench.settle()
        bench.number += 1
        t = f"PGU-{bench.number}"
        bench.made.append(t)
        fixture.seed_postgres_ticket(self.board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director")
        check(short(self.board.act(t, "route", "director", state="in_progress", assignee="app")) == "ok", f"{t} routed")
        if documentation:
            check(short(self.board.act(t, "set_workflow_flags", "director", needs_documentation=True)) == "ok",
                  f"{t} needs documentation review")
        candidate = authored(self.world, (parents or [self.world.main()])[0], {f"src/{t.lower()}.py": f"# {t}\n"},
                             f"{t}: the work", "app")
        self.world.publish(f"roles/app/{t.lower()}", candidate)
        check(short(self.board.act(t, "submit_to_audit", "app", commit_hash=candidate)) == "ok", f"app submits {t}")
        return t, candidate

    def signed_off(self, t: str, candidate: str, text: str = "") -> str:
        """The Librarian's documentation commit on `candidate`, published at the ticket's ref and signed off."""
        docs = authored(self.world, candidate, {f"docs/{t.lower()}.md": text or f"How to use {t}.\n"}, f"{t} docs",
                        "librarian")
        ref = candidate_ref("librarian", t, candidate)
        self.world.publish(ref, docs)
        said = short(self.board.act(t, "documentation_sign_off", "librarian", commit_hash=docs, ref=ref))
        check(said == "ok" and row(self.board, t)["commit"] == docs and row(self.board, t)["state"] == "audit",
              f"the Librarian signs off {t} with its documentation commit, now the candidate, in Audit: {said}")
        return docs

    def returned_for_documentation(self, t: str) -> None:
        """Audit's documentation-only return (SYRD-719): back to the Librarian, keeping the audited candidate."""
        kept = row(self.board, t)["commit"]
        said = short(self.board.act(t, "audit_documentation_kick_back", "audit", reason="The guide misnames a flag."))
        now = row(self.board, t)
        check(said == "ok" and now["state"] == "documentation" and now["assignee"] == "librarian" and now["commit"] == kept,
              f"Audit returns {t} to the Librarian for documentation alone, the candidate kept: {said} {now}")

    def layers(self, t: str) -> list[str]:
        return json.loads(self.board.sql(
            "SELECT coalesce(json_agg(commit_hash ORDER BY position), '[]')::text FROM "
            f"ticket_board.ticket_documentation_layers WHERE ticket_id = '{t}';"))

    def audited(self, t: str) -> None:
        check(short(self.board.act(t, "audit_sign_off", "audit")) == "ok" and row(self.board, t)["state"] == "publication",
              f"Audit signs off {t}, and it waits for the Publisher")


def recorded_pair(pair: Pair) -> None:
    world, board = pair.world, pair.board
    before = world.main()
    t, candidate = pair.submitted()
    docs = pair.signed_off(t, candidate)
    pair.audited(t)
    result = pair.bench.publish(t)
    after = row(board, t)
    check(result["outcome"] == "published" and after["state"] == "director_review" and after["commit"] == docs
          and after["audit"], f"the recorded pair is published, on to Final Sign-Off, sign-offs kept: {result} {after}")
    check(world.main() == docs and git("--git-dir", str(world.public), "rev-parse", f"{docs}^") == candidate
          and git("--git-dir", str(world.public), "rev-parse", f"{candidate}^") == before
          and git("--git-dir", str(world.public), "rev-list", "--count", f"{before}..main") == "2",
          "public main is the pair, both SHAs as approved, in order, one fast-forward on the tip")
    check(author(world, candidate) == "app <app@example.invalid>"
          and author(world, docs) == "librarian <librarian@example.invalid>",
          "each commit keeps its own author: the implementer's work is not the Librarian's, nor the other way")
    check(world.cache_main() == docs, "the board's trusted cache reads it too")
    said = comments(board, t)
    check(len(said) == 1 and said[0].startswith(f"Published {docs}") and "board-recorded documentation pair" in said[0]
          and candidate in said[0] and docs in said[0] and "app <app@example.invalid>" in said[0]
          and "librarian <librarian@example.invalid>" in said[0],
          f"one evidence comment, naming both commits and both authors: {said}")
    journal = pb.attempts(board, t)
    check(journal[-1]["facts"].get("documentation_pair", {}).get("implementer") == candidate,
          f"the attempt's journal records the pair it published: {journal[-1]['facts'].get('documentation_pair')}")


def chain_on(world, top: str, count: int) -> list[str]:
    """The `count` commits under and including `top` on public main, bottom first."""
    return git("--git-dir", str(world.public), "rev-list", "--reverse", f"-n{count}", top).split()


def corrected_after_documentation_return(pair: Pair) -> None:
    """Audit returns the documentation alone; the Librarian corrects it with a second documentation commit on the
    first. The board recorded both layers, so the three commits publish as they stand."""
    world, board = pair.world, pair.board
    before = world.main()
    t, candidate = pair.submitted()
    first = pair.signed_off(t, candidate)
    pair.returned_for_documentation(t)
    check(pair.layers(t) == [first], f"the return keeps the recorded layer: {pair.layers(t)}")
    second = pair.signed_off(t, first, text=f"How to use {t}, with the flag named right.\n")
    check(pair.layers(t) == [first, second], f"the correction extends the recorded layer: {pair.layers(t)}")
    pair.audited(t)
    result = pair.bench.publish(t)
    check(result["outcome"] == "published" and row(board, t)["state"] == "director_review"
          and chain_on(world, "main", 4) == [before, candidate, first, second],
          f"the implementer's commit and both documentation commits are on main, in order, on the old tip: {result}")
    check([author(world, c) for c in (candidate, first, second)]
          == ["app <app@example.invalid>", "librarian <librarian@example.invalid>", "librarian <librarian@example.invalid>"],
          "each commit keeps its author")
    said = comments(board, t)
    check(len(said) == 1 and "3 commits in that one push" in said[0] and all(c in said[0] for c in (candidate, first, second)),
          f"the evidence names all three commits: {said}")
    check([step["commit"] for step in pb.attempts(board, t)[-1]["facts"]["documentation_pair"]["chain"]]
          == [candidate, first, second], "the journal records the chain it published")


def no_change_after_documentation_return(pair: Pair) -> None:
    """Audit returns the documentation alone; the Librarian disagrees and takes no change. The candidate is still the
    recorded documentation commit on the implementer's, and publishes as the pair."""
    world, board = pair.world, pair.board
    before = world.main()
    t, candidate = pair.submitted()
    docs = pair.signed_off(t, candidate)
    pair.returned_for_documentation(t)
    said = short(board.act(t, "documentation_no_change", "librarian", reason="The flag is named right; see --help."))
    check(said == "ok" and row(board, t)["commit"] == docs and pair.layers(t) == [docs],
          f"no change keeps the candidate and its recorded layer: {said} {pair.layers(t)}")
    pair.audited(t)
    result = pair.bench.publish(t)
    check(result["outcome"] == "published" and chain_on(world, "main", 3) == [before, candidate, docs]
          and "2 commits in that one push" in comments(board, t)[0],
          f"no change after a documentation return publishes the recorded pair: {result} {comments(board, t)}")


def rework_starts_the_layer_again(pair: Pair) -> None:
    """The implementer's own revision ends the recorded layer; the Librarian's next commit starts a new one on it."""
    world, board = pair.world, pair.board
    before = world.main()
    t, candidate = pair.submitted()
    pair.signed_off(t, candidate)
    check(short(board.act(t, "audit_kick_back", "audit", reason="A test is missing.")) == "ok", f"{t} kicked back")
    rework = authored(world, before, {f"src/{t.lower()}.py": f"# {t}, tested\n"}, f"{t}: reworked", "app")
    world.publish(f"roles/app/{t.lower()}", rework)
    check(short(board.act(t, "submit_to_audit", "app", commit_hash=rework)) == "ok" and pair.layers(t) == [],
          f"the implementer's resubmission clears the recorded layer: {pair.layers(t)}")
    docs = pair.signed_off(t, rework)
    check(pair.layers(t) == [docs], f"the Librarian's commit on the rework is the whole layer: {pair.layers(t)}")
    pair.audited(t)
    result = pair.bench.publish(t)
    check(result["outcome"] == "published" and chain_on(world, "main", 3) == [before, rework, docs],
          f"the rework and its documentation commit publish, and nothing of the earlier round: {result}")


def record_must_match(pair: Pair) -> None:
    """The Publisher publishes the record only where it is the candidate's history, link by link: a layer whose
    recorded parent is not the commit under it, or a record whose implementer commit is not where the layer's history
    goes, is not the recorded pair, and is returned as any other candidate of several commits is."""
    world, board = pair.world, pair.board
    for how in ("a layer's parent", "the implementer commit", "the producer", "the top layer"):
        tip = world.main()
        t, candidate = pair.submitted()
        first = pair.signed_off(t, candidate)
        pair.returned_for_documentation(t)
        pair.signed_off(t, first, text=f"How to use {t}, corrected.\n")
        pair.audited(t)
        if how == "a layer's parent":
            board.sql(f"UPDATE ticket_board.ticket_documentation_layers SET parent_commit = '{'e' * 40}' "
                      f"WHERE ticket_id = '{t}' AND position = 2;")
        elif how == "the producer":
            board.sql(f"UPDATE ticket_board.ticket_audit_returns SET producer_commit = '{'e' * 40}' "
                      f"WHERE ticket_id = '{t}';")
        elif how == "the top layer":
            board.sql(f"DELETE FROM ticket_board.ticket_documentation_layers WHERE ticket_id = '{t}' AND position = 2;")
        else:
            other = authored(world, tip, {"src/other.py": "o\n"}, "another commit on the tip", "app")
            git("--git-dir", str(world.cache), "fetch", "-q", str(world.dev), other)
            board.sql(f"UPDATE ticket_board.ticket_audit_returns SET producer_commit = '{other}', base_commit = '{other}' "
                      f"WHERE ticket_id = '{t}'; UPDATE ticket_board.ticket_documentation_layers SET parent_commit = "
                      f"'{other}' WHERE ticket_id = '{t}' AND position = 1;")
        if how != "the implementer commit":
            with psycopg.connect(board.listener_url, autocommit=True) as conn:
                read = conn.execute("SELECT ticket_board.publication_documentation_pair(%s)", (t,)).fetchone()[0]
            check(read is None, f"with {how} misrecorded, the board reads no documentation pair: {read}")
        pb.returned(pair.bench, t, f"is 3 commits on public main ({tip[:12]})", "squash it onto")
        check(world.main() == tip, f"with {how} misrecorded, nothing is pushed")


def unrecorded_pair(pair: Pair) -> None:
    world = pair.world
    first = world.commit([world.main()], {"src/one.py": "1\n"}, "first of two")
    t, candidate = pair.submitted(documentation=False, parents=[first])
    pair.audited(t)
    pb.returned(pair.bench, t, f"is 2 commits on public main ({world.main()[:12]})", "squash it onto")
    # Two commits shaped like a documentation pair, but the board recorded no documentation sign-off.
    t, candidate = pair.submitted(documentation=False)
    on_top = authored(world, candidate, {f"docs/{t.lower()}.md": "docs\n"}, "docs the implementer added", "app")
    world.publish(f"roles/app/{t.lower()}", on_top)
    check(short(pair.board.act(t, "audit_kick_back", "audit", reason="resubmit")) == "ok"
          and short(pair.board.act(t, "submit_to_audit", "app", commit_hash=on_top)) == "ok", f"{t} resubmitted")
    pair.audited(t)
    pb.returned(pair.bench, t, f"is 2 commits on public main ({world.main()[:12]})", "squash it onto")


def stale_pair(pair: Pair) -> None:
    world = pair.world
    t, candidate = pair.submitted()
    pair.signed_off(t, candidate)
    pair.audited(t)
    moved = pair.bench.integrate({"src/elsewhere.py": "x\n"})
    pb.returned(pair.bench, t, "is the board-recorded documentation pair", f"is not one commit on public main ({moved[:12]})",
                "rebuild the work on")


def push_rechecks_the_pair(pair: Pair) -> None:
    """The push repository proves the pair again from the bundle: told a wrong parent for the implementer's commit
    there, the Publisher escalates and pushes nothing."""
    t, candidate = pair.submitted()
    pair.signed_off(t, candidate)
    pair.audited(t)

    def answer(argv):
        if (len(argv) > 3 and "/push-" in argv[2] and argv[3:7] == ["rev-list", "--parents", "-n", "1"]
                and argv[-1] == candidate):
            return subprocess.CompletedProcess(argv, 0, f"{candidate} {'f' * 40}\n", "")
        return None

    pb.escalated(pair.bench, t, "helper", pb.Runner(answer), "the bundle unpacked as", "nothing was pushed")


def no_change(pair: Pair) -> None:
    world, board = pair.world, pair.board
    before = world.main()
    t, candidate = pair.submitted()
    said = short(board.act(t, "documentation_no_change", "librarian", reason="Nothing a person reads changed."))
    check(said == "ok" and row(board, t)["commit"] == candidate, f"no documentation change keeps the candidate: {said}")
    pair.audited(t)
    result = pair.bench.publish(t)
    check(result["outcome"] == "published" and world.main() == candidate
          and git("--git-dir", str(world.public), "rev-parse", f"{candidate}^") == before
          and "documentation pair" not in comments(board, t)[0],
          f"a ticket with no documentation change publishes its one commit, as before: {result}")


def main() -> int:
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    document = with_publisher(with_documentation_gate(example(), librarian={"runtime": "claude"}))
    with temporary_cluster(prefix="syrd848-", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(
            prefix="syrd848.") as tmp:
        bench = Bench(cluster, "pair", Path(tmp), document)
        try:
            pair = Pair(bench)
            recorded_pair(pair)
            corrected_after_documentation_return(pair)
            no_change_after_documentation_return(pair)
            rework_starts_the_layer_again(pair)
            record_must_match(pair)
            unrecorded_pair(pair)
            stale_pair(pair)
            push_rechecks_the_pair(pair)
            no_change(pair)
        finally:
            bench.close()
    print(f"publisher_documentation_pair_test: {CHECKS} checks ok (and {pb.CHECKS} of the shared fixture's)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
