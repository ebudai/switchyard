#!/usr/bin/env python3
"""SYRD-849: a research finding with a commit reaches main through the Publisher.

publisher_board_test's real world is used: a bare forge, the board's trusted cache of it, and the real Publisher
(publisher.publish_ticket over the listener's functions). The board is built from main's tree before this change and
upgraded with this tree's migrations. It declares the research stage, the documentation gate and the Publisher.

* **The finding moves are composed** with the Publisher whichever composer runs first, and taken out with it.
* **A findings commit:**
  - close_on_finding is refused, naming publish-finding;
  - the Director's publish_finding takes it into publication, for the Publisher;
  - the Publisher publishes exactly that commit as a fast-forward and moves the ticket to done. No Audit code
    sign-off is asked of it, and it skips Documentation and Final Sign-Off.
* **A stale base** (main advanced) is returned to the researcher, in Research, who rebases, resubmits and is published.
* **A failed publication** is the Director's:
  - the ordinary publication_kick_back is refused for a finding;
  - finding_publication_kick_back returns it to the researcher.
* **Refusals:**
  - publish_finding on a no-commit finding, which closes on its finding as before;
  - publish_finding on a ticket that is not a research ticket;
  - publish_finding on a commit that is not the finding submitted;
  - the finding's kick-back on a ticket that was never accepted as one.
* **Code tickets are unchanged:** an ordinary approved candidate still goes publication -> Final Sign-Off.
"""

from __future__ import annotations

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

import publisher_board_test as pb  # noqa: E402
from publisher_board_test import Bench, Runner, comments, example, failed, fixture, git, row, short  # noqa: E402
from publisher_board_test import temporary_cluster  # noqa: E402
from scripts.ticket_board import research_stage as rs  # noqa: E402
from scripts.ticket_board.documentation_gate import with_documentation_gate  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher, without_publisher  # noqa: E402

CHECKS = 0
MIGRATION = "scripts/ticket_board/migrations/pgu1054_syrd849_findings_publication.sql"


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def research(doc: dict) -> dict:
    return rs.with_research_stage(doc, researcher={"runtime": "claude"})


def finding_moves(doc: dict) -> list[tuple[str, str, str, list[str]]]:
    return sorted((t["from"], t["to"], t["action"], t["actors"]) for t in doc["transitions"]
                  if t["action"] in rs.PUBLICATION_ACTIONS)


def test_composition() -> None:
    first, second = with_publisher(research(example())), research(with_publisher(example()))
    expected = [("analysis", "publication", rs.PUBLISH, ["director"]),
                ("publication", "done", rs.PUBLISHED, ["publisher"]),
                ("publication", "research", rs.PUBLICATION_RETURN, ["publisher"]),
                ("publication_failed", "research", rs.PUBLICATION_KICK_BACK, ["director"])]
    check(finding_moves(first) == expected and finding_moves(second) == expected,
          f"with the Publisher, the finding's moves are composed in either order: {finding_moves(first)}")
    check(finding_moves(research(example())) == [] and finding_moves(without_publisher(first)) == [],
          "without the Publisher there are none, and turning it off takes them out")
    check(finding_moves(rs.without_research_stage(first)) == [], "turning research off takes them out too")
    broken = with_publisher(research(example()))
    next(t for t in broken["transitions"] if t["action"] == rs.PUBLISHED)["to"] = "director_review"
    try:
        from scripts.ticket_board.workflow_config import validate
        validate(broken)
        said = "accepted"
    except ValueError as exc:
        said = str(exc)
    check("exactly as composed" in said, f"a published finding sent anywhere else is refused: {said}")


def main_commit() -> str:
    adding = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--", MIGRATION],
                            check=True, capture_output=True, text=True).stdout.split()
    return f"{adding[-1]}^" if adding else "HEAD"


class Research:
    """Research tickets on the bench: briefed, started, a finding submitted and signed off, back with the Director."""

    def __init__(self, bench: Bench) -> None:
        self.bench, self.board, self.world = bench, bench.board, bench.world
        self.number = 700

    def seeded(self) -> str:
        self.number += 1
        t = f"PGU-{self.number}"
        fixture.seed_postgres_ticket(self.board.admin, t, title=f"Research {t}", state="analysis", assignee="director")
        said = short(self.board.act(t, "set_research_brief", "director", question="Which encoding?", timebox="2h",
                                    usable_answer="Its name, with a sample decoded."))
        check(said == "ok", f"{t} briefed: {said}")
        said = short(self.board.act(t, rs.START, "director"))
        check(said == "ok", f"{t} started: {said}")
        return t

    def findings_commit(self, t: str, text: str = "# Finding\nUTF-16LE.\n") -> str:
        commit = self.world.commit([self.world.main()], {f"docs/research/{t}.md": text}, f"{t}: finding")
        self.world.publish(f"roles/researcher/{t.lower()}", commit)
        return commit

    def submitted(self, t: str, commit: str | None) -> None:
        said = (short(self.board.act(t, rs.SUBMIT, "researcher", commit_hash=commit)) if commit else
                short(self.board.act(t, rs.SUBMIT_NO_CODE, "researcher", reason="Nothing to keep: 3.1 GB, du -sb.")))
        check(said == "ok" and self.board.where(t) == ["research_review", "audit"], f"{t}'s finding to review: {said}")
        said = short(self.board.act(t, rs.SIGN_OFF, "audit"))
        check(said == "ok" and self.board.where(t) == ["analysis", "director"], f"Audit signs it off: {said}")

    def accepted(self, t: str) -> None:
        said = short(self.board.act(t, rs.PUBLISH, "director", reason="UTF-16LE; the importer ticket follows."))
        check(said == "ok" and self.board.where(t) == ["publication", "publisher"],
              f"the Director accepts the finding into publication, for the Publisher: {said} {self.board.where(t)}")


def published(lab: Research) -> None:
    board, world = lab.board, lab.world
    t = lab.seeded()
    commit = lab.findings_commit(t)
    lab.submitted(t, commit)
    before = row(board, t)
    said = short(board.act(t, rs.CLOSE, "director", reason="Done."))
    check("publishes it through the Publisher" in said and f"publish-finding {t}" in said and row(board, t) == before,
          f"a finding with a commit is not closed around the Publisher: {said}")
    tip = world.main()
    lab.accepted(t)
    result = lab.bench.publish(t)
    after = row(board, t)
    check(result["outcome"] == "published" and board.where(t)[0] == "done" and after["commit"] == commit,
          f"the Publisher publishes the finding and the ticket is done: {result} {board.where(t)}")
    check(world.main() == commit and git("--git-dir", str(world.public), "rev-parse", f"{commit}^") == tip
          and git("--git-dir", str(world.public), "show", "--format=", "--name-only", commit) == f"docs/research/{t}.md",
          "public main is the findings commit, one fast-forward, changing only the ticket's findings file")
    said = comments(board, t)
    check(len(said) == 1 and said[0].startswith(f"Published {commit}"), f"one evidence comment: {said}")
    check([a["state"] for a in pb.attempts(board, t)] == ["published"], "one attempt, published")
    check(pb.attempts(board, t)[-1]["facts"].get("candidate_full") == commit,
          "the attempt published exactly the findings commit, and its one move took the ticket from publication to done")


def stale(lab: Research) -> None:
    board, world = lab.board, lab.world
    t = lab.seeded()
    commit = lab.findings_commit(t)
    built_on = world.main()
    lab.submitted(t, commit)
    lab.accepted(t)
    moved = lab.bench.integrate({"src/elsewhere.py": "x\n"})
    lab.bench.notices()
    result = lab.bench.publish(t)
    said = comments(board, t)
    check(result["outcome"] == "returned" and board.where(t) == ["research", "researcher"],
          f"a stale finding goes back to its researcher, in Research: {result} {board.where(t)}")
    check(len(said) == 1 and "public main advanced" in said[0] and built_on[:12] in said[0] and moved[:12] in said[0],
          f"saying why: {said}")
    sent = lab.bench.notices()
    check(any(target == "pgu-researcher:0.0" for target, _ in sent), f"and the researcher is told: {sent}")
    check(world.main() == moved, "nothing was pushed")
    rebased = lab.findings_commit(t, "# Finding\nUTF-16LE, rebased.\n")
    lab.submitted(t, rebased)
    lab.accepted(t)
    result = lab.bench.publish(t)
    check(result["outcome"] == "published" and world.main() == rebased and board.where(t)[0] == "done",
          f"rebased and resubmitted, it is published: {result}")


def failed_publication(lab: Research) -> None:
    board, world = lab.board, lab.world
    t = lab.seeded()
    commit = lab.findings_commit(t)
    lab.submitted(t, commit)
    lab.accepted(t)
    permission = "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository."
    result = lab.bench.publish(t, Runner(lambda argv: failed(argv, permission) if "push" in argv else None))
    check(result["outcome"] == "escalated" and board.where(t) == ["publication_failed", "director"],
          f"a refused push is the Director's, as for any candidate: {result} {board.where(t)}")
    said = short(board.act(t, "publication_kick_back", "director", reason="Rebuild it."))
    check("is a finding" in said and f"finding-publication-kick-back {t}" in said
          and board.where(t)[0] == "publication_failed",
          f"the ordinary kick-back to Implementation is refused for a finding: {said}")
    said = short(board.act(t, rs.PUBLICATION_KICK_BACK, "director", reason="The forge refused it; rebuild on main."))
    check(said == "ok" and board.where(t) == ["research", "researcher"],
          f"the finding's kick-back returns it to the researcher: {said} {board.where(t)}")
    check(world.main() != commit, "nothing was published")
    check(short(board.act(t, "defer", "director")) == "ok", "put down")


def refusals(lab: Research) -> None:
    board = lab.board
    t = lab.seeded()
    lab.submitted(t, None)
    before = row(board, t)
    said = short(board.act(t, rs.PUBLISH, "director", reason="Publish it."))
    check("has no findings commit to publish" in said and f"close-on-finding {t}" in said and row(board, t) == before,
          f"a no-commit finding has nothing to publish: {said}")
    said = short(board.act(t, rs.CLOSE, "director", reason="3.1 GB."))
    check(said == "ok" and board.where(t)[0] == "done", f"and closes on its finding as before: {said}")
    plain = "PGU-799"
    fixture.seed_postgres_ticket(board.admin, plain, title="Plain", state="analysis", assignee="director",
                                 commit_hash=lab.world.main())
    said = short(board.act(plain, rs.PUBLISH, "director", reason="Publish it."))
    check("for research tickets" in said, f"publish_finding is for research tickets only: {said}")
    t = lab.seeded()
    commit = lab.findings_commit(t)
    lab.submitted(t, commit)
    other = lab.findings_commit(t, "# Another\n")
    board.sql(f"BEGIN; SET LOCAL session_replication_role = replica; "  # the commit changed under the record
              f"UPDATE ticket_board.tickets SET commit_hash = '{other}' WHERE id = '{t}'; COMMIT;")
    said = short(board.act(t, rs.PUBLISH, "director", reason="Publish it."))
    check("is not the findings commit its researcher submitted" in said and board.where(t)[0] == "analysis",
          f"only the finding submitted is published: {said}")
    board.sql(f"BEGIN; SET LOCAL session_replication_role = replica; UPDATE ticket_board.tickets "
              f"SET state = 'publication_failed', assignee = 'director', commit_hash = '{commit}' WHERE id = '{t}'; COMMIT;")
    said = short(board.act(t, rs.PUBLICATION_KICK_BACK, "director", reason="x"))
    check("was not accepted for publication as a finding" in said and board.where(t)[0] == "publication_failed",
          f"the finding's kick-back is only for a finding the Director accepted: {said}")
    check(short(board.act(t, "defer", "director")) == "ok", "put down")
    code, candidate = lab.bench.ticket()
    result = lab.bench.publish(code)
    check(result["outcome"] == "published" and row(board, code)["state"] == "director_review",
          f"an ordinary candidate still goes on to Final Sign-Off: {result}")
    board.sql(f"BEGIN; SET LOCAL session_replication_role = replica; UPDATE ticket_board.tickets "
              f"SET state = 'publication_failed', assignee = 'director' WHERE id = '{code}'; COMMIT;")
    said = short(board.act(code, rs.PUBLICATION_KICK_BACK, "director", reason="x"))
    check("for research tickets" in said and row(board, code)["state"] == "publication_failed",
          f"the finding's kick-back is not a code ticket's: {said}")


def main() -> int:
    test_composition()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    document = with_publisher(research(with_documentation_gate(example(), librarian={"runtime": "claude"})))
    with temporary_cluster(prefix="syrd849-", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(
            prefix="syrd849.") as tmp:
        tree = Path(tmp) / "main"
        tree.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", main_commit(), "scripts"], check=True,
                                 capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True)
        check(not (tree / MIGRATION).exists(), "the board is built from main's tree, without this migration")
        bench = Bench(cluster, "findings", Path(tmp), document, before=tree)
        try:
            bench.notices()
            lab = Research(bench)
            published(lab)
            stale(lab)
            failed_publication(lab)
            refusals(lab)
        finally:
            bench.close()
    print(f"research_publication_test: {CHECKS} checks ok (and {pb.CHECKS} of the shared fixture's)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
