#!/usr/bin/env python3
"""SYRD-844: a reviewer's mutation run is scoped to what the ticket changed, not the whole corpus.

On sapi, Audit ran hundreds of mutants against the whole test corpus on ordinary tickets, half an hour or more
each time, because nothing in the shared reviewer guidance said how big a mutation run should be. The rule, the
same in every body a reviewer reads:

* mutants go on the lines and behaviours the candidate adds or changes, and run against the tests covering that
  area, never the whole corpus;
* a few semantic mutants, each answering a review question, beat hundreds of syntactic ones, and the
  implementer's recorded mutants are reused rather than regenerated;
* the limit is on scope, not on time: no duration cap and no reason owed before a long run (the User's
  direction on the ticket);
* a whole-corpus run is the exception: test tier `full`, a project that asks for one, or a milestone pass;
* a survivor is a finding, whatever the run's size.

The carriers are the board skill and board guide (Audit has no generated role prompt: `switchyard new` and
`add-role` ship one only for the Director, Router and Librarian, so these are what Audit reads), the Director
guide and skill, the shared methodology, and the LLM operations index. The board skill and guide also tie the
rule to the ticket's test tier. Main is checked first, from git: it says none of this.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import board_skill  # noqa: E402

#: Main before this ticket, inserted from `git rev-parse`.
BEFORE = "cdc3a741e65d908951b72a31920a0e6423f22f40"

BOARD_SKILL = board_skill.BOARD_SKILL.relative_source
DIRECTOR_SKILL = board_skill.DIRECTOR_SKILL.relative_source
BOARD_GUIDE = "docs/onboarding/switchyard-board-guide.md"
DIRECTOR_GUIDE = "docs/onboarding/switchyard-director-guide.md"
METHODOLOGY = "docs/onboarding/adversarial-collaborative-methodology.md"
LLM_OPERATIONS = "docs/llm-operations.md"

# Patterns rather than sentences: each body is written for its own reader. Matched on lowercased,
# whitespace-flattened text.
ON_THE_CHANGE = ("mutants go on what the ticket changed",
                 r"(?:lines and behaviours|on what) the (?:candidate adds or changes|ticket changed)")
CLAUSES = (
    ON_THE_CHANGE,
    ("run against the tests covering that area", r"tests covering that area"),
    ("never against the whole corpus", r"never (?:run them against )?the whole corpus"),
    ("a few semantic mutants", r"a few semantic mutants"),
    ("each answers a review question", r"each answer(?:s|ing)? a (?:review )?question|each answering a question"),
    ("recorded mutants are reused, not regenerated",
     r"reuse the implementer's recorded mutants rather than regenerating|recorded mutants reused"),
    ("scope, not time", r"not how long the run takes|scope, not on time"),
)
FULL_ONLY = (
    ("over hundreds of syntactic ones", r"beat hundreds of syntactic ones"),
    ("a survivor is a finding", r"survivor is (?:still )?a finding\**, whatever the run's size"),
)
WHOLE_CORPUS = r"whole-corpus (?:mutation )?run is (?:the exception|for)[^.]{0,200}"
EXCEPTIONS = (("test tier full", "`full`"), ("a project that asks", "asks for one"), ("a milestone pass", "milestone"))
# A duration cap, or a reason owed before a long run: the User ruled both out.
TIME_CAP = r"\b\d+\s*(?:s|secs?|seconds?|m|mins?|minutes?|h|hours?)\b|reason (?:before|ahead of) (?:a|any) long run"
TIER_TIE = (
    ("partial or no tier keeps the scoped rule", r"`partial`, or no tier, keeps it to what the ticket changed"),
    ("full lets the reviewer run the wider plan", r"`full` lets the reviewer run the wider plan"),
)

FULL_CARRIERS = (BOARD_SKILL, BOARD_GUIDE, DIRECTOR_GUIDE, DIRECTOR_SKILL, METHODOLOGY)
CARRIERS = FULL_CARRIERS + (LLM_OPERATIONS,)


def check(condition: object, detail: str) -> None:
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def at_main(path: str) -> str:
    """A carrier as main had it before this ticket, read from git."""
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{BEFORE}:{path}"], check=True, capture_output=True,
                          text=True).stdout


def here(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def missing(text: str, full: bool) -> list[str]:
    """The clauses of the rule this body does not state."""
    flat = _flat(text)
    lost = [label for label, pattern in CLAUSES + (FULL_ONLY if full else ()) if not re.search(pattern, flat)]
    found = re.search(WHOLE_CORPUS, flat)
    if not found:
        lost.append("a whole-corpus run is the exception")
    else:
        lost += [label for label, word in EXCEPTIONS if word not in found.group(0)]
    return lost


def rule_block(text: str) -> str:
    """The paragraph or list that states the rule: the blank-line-separated block holding its first clause."""
    blocks = [block for block in re.split(r"\n[ \t]*\n", text) if re.search(ON_THE_CHANGE[1], _flat(block))]
    check(blocks, "the rule's first clause is present")
    return blocks[0]


# --- main first ------------------------------------------------------------------------


def test_main_does_not_scope_a_reviewers_mutation_run() -> None:
    """The bug: no carrier on main says where mutants go, what they run against, or when the corpus is in scope."""
    for path in CARRIERS:
        lost = missing(at_main(path), path in FULL_CARRIERS)
        for label in (ON_THE_CHANGE[0], "never against the whole corpus", "a whole-corpus run is the exception"):
            check(label in lost, f"main's {path} already says {label!r}")
    for path in (BOARD_SKILL, BOARD_GUIDE):
        flat = _flat(at_main(path))
        check(not any(re.search(pattern, flat) for _, pattern in TIER_TIE), f"main's {path} already ties the tier")


# --- the rule, in each body a reviewer reads ------------------------------------------


def test_each_carrier_states_the_whole_rule() -> None:
    for path in CARRIERS:
        lost = missing(here(path), path in FULL_CARRIERS)
        check(lost == [], f"{path} does not state: {lost}")


def test_the_rule_sets_no_time_limit() -> None:
    """Scope only: no duration cap and no reason owed before a long run, in the text that states the rule."""
    for path in CARRIERS:
        block = rule_block(here(path))
        cap = re.search(TIME_CAP, block, re.IGNORECASE)
        check(cap is None, f"{path}: the mutation rule sets a time limit: {cap.group(0) if cap else ''!r}")


def test_the_board_skill_and_guide_tie_the_rule_to_the_test_tier() -> None:
    for path in (BOARD_SKILL, BOARD_GUIDE):
        flat = _flat(here(path))
        lost = [label for label, pattern in TIER_TIE if not re.search(pattern, flat)]
        check(lost == [], f"{path} does not tie the mutation rule to the test tier: {lost}")
        tier = flat.find("test tier")
        tie = re.search(TIER_TIE[0][1], flat)
        check(0 <= tier < tie.start() and tie.start() - tier <= 1200,
              f"{path}: the tie is not in the paragraph that explains the tier")


def test_the_board_guide_states_it_in_the_test_evidence_section() -> None:
    flat = _flat(here(BOARD_GUIDE))
    start = flat.find("## test evidence: reuse what is exact, rerun for a reason")
    end = flat.find("## the board skill", start)
    check(0 <= start < end, "the board guide keeps its Test evidence section")
    check(missing(flat[start:end], True) == [], f"the Test evidence section lacks: {missing(flat[start:end], True)}")


def test_skills_carry_no_ticket_ids() -> None:
    for path in (BOARD_SKILL, DIRECTOR_SKILL):
        check(not re.search(r"\bSYRD-\d|\bPGU-\d", here(path)), f"{path} names a ticket")


def test_the_rule_reaches_every_installed_board_skill() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd844-skill.") as tmp:
        home = Path(tmp)
        tree = home / "release-c0ffee"
        for skill in board_skill.CANONICAL_SKILLS:
            target = tree / skill.relative_source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / skill.relative_source).read_bytes())
        (tree / board_skill.RELEASE_MARKER_NAME).write_text(json.dumps({"commit": "c0ffee"}) + "\n", encoding="utf-8")
        results = board_skill.install_board_skill(home=home, source=board_skill.default_source(tree))
        check(results and all(result.action == "installed" for result in results),
              f"board skill install: {[result.action for result in results]}")
        adapters = list(board_skill.adapter_paths(home, board_skill.BOARD_SKILL))
        check(adapters, "the board skill has installed adapters")
        for runtime, path in adapters:
            lost = missing(path.read_text(encoding="utf-8"), True)
            check(lost == [], f"{runtime} board skill lacks {lost}")


def main() -> int:
    run_module_tests(globals(), first=("test_main_does_not_scope_a_reviewers_mutation_run",))
    print("review_mutation_scope_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
