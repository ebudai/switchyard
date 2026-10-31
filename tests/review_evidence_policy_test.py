#!/usr/bin/env python3
"""SYRD-720: Audit and the Director reuse exact test evidence and rerun for a reason.

Repeating an implementer's complete suite at every review stage multiplied
latency without adding confidence. The policy: the implementer runs and records
the complete validation exactly; Audit verifies that the record applies and runs
focused checks chosen from the review's risk; Final Sign-Off verifies the exact
candidate, decisions and gates. Neither reruns the complete suite merely because
the ticket entered its stage. A rerun needs a concrete reason the result could
differ, recorded on the ticket before it starts.

The policy is operating guidance only, so it lives in hand-written bodies: the
board guide (Audit's onboarding) and board skill (Audit's prompt), the Director
guide and skill, the shared methodology, the Librarian guide and the LLM
operations index. This module pins each clause in each carrier, pins the
retired wording that told every reviewer to rerun everything, and pushes the
board skill through the installer into every CLI tree. SYRD-47's suite
(`director_verification_policy_test.py`) keeps pinning the Director's half.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import project_onboarding  # noqa: E402
from scripts.ticket_board import board_skill  # noqa: E402

ONBOARDING = ROOT / "docs" / "onboarding"
BOARD_GUIDE = ONBOARDING / "switchyard-board-guide.md"
DIRECTOR_GUIDE = ONBOARDING / "switchyard-director-guide.md"
METHODOLOGY = ONBOARDING / "adversarial-collaborative-methodology.md"
LIBRARIAN_GUIDE = ONBOARDING / "switchyard-librarian-guide.md"
BOARD_SKILL = ROOT / board_skill.BOARD_SKILL.relative_source
DIRECTOR_SKILL = ROOT / board_skill.DIRECTOR_SKILL.relative_source
LLM_OPERATIONS = ROOT / "docs" / "llm-operations.md"
DOCUMENTATION_MAP = ROOT / "docs" / "documentation-map.md"

# Patterns rather than sentences: each body is written for its own reader, but
# none may lose a clause. Matched case-insensitively on whitespace-flattened text.
DEFAULT = (
    ("no complete rerun merely for entering the stage",
     r"(?:not|n't) rerun (?:the|that|the implementer's or audit's) complete suite merely because the ticket "
     r"(?:entered|reached) (?:audit|final sign-off|their stage)"),
    ("focused checks chosen from the review's risk", r"focused checks? (?:of (?:its|your) own|chosen from|only where|where)"),
    ("reuse exact evidence", r"\breuse"),
    ("a reason is recorded before a complete rerun",
     r"(?:record (?:that|the) reason[^.]{0,60}before you start|recorded first\.\*\* before you start one"
     r"|before starting a complete-suite rerun[^.]{0,60}record that reason)"),
)

# The concrete triggers, one family each, in plain language.
TRIGGERS = (
    ("candidate commit, tree or base changed", r"non-mechanical rebase"),
    ("dependencies, toolchain, schema or runtime changed", r"(?:toolchain|schema)[^.]{0,80}(?:runtime|changed)"),
    ("the recorded run is incomplete or unbounded", r"incomplete, unbounded"),
    ("baseline and candidate failures not compared", r"(?:compared|compare)[^.]{0,40}(?:with the base|adequately)|failures (?:were )?not compared"),
    ("an untested risk, mutation survivor or security boundary", r"mutation survivor, (?:a )?security boundary"),
    ("suspected flakiness", r"flak"),
    ("an acceptance property the evidence cannot establish", r"acceptance property cannot be established"),
)

NOT_REASONS = (
    ("a new stage is not a reason", r"(?:entered a new stage|a new stage|entering (?:a new stage|final sign-off))"),
    ("another copy is not a reason", r"own copy of an?(?: already)? exact"),
    ("elapsed time is not a reason", r"elapsed time"),
    ("generic caution is not a reason", r"caution (?:that )?names nothing"),
)

EXAMPLES = (
    ("unchanged candidate", r"unchanged candidate"),
    ("rebase", r"rebase"),
    ("Librarian documentation commit", r"librarian (?:documentation )?commit"),
    ("migration or runtime change", r"migration or runtime change"),
    ("flaky test", r"flaky test"),
    ("baseline-equivalent failures", r"baseline-equivalent failures"),
    ("security finding", r"security finding"),
)

IMPLEMENTER_RECORD = (
    ("candidate commit and tree", r"commit (?:and (?:its )?tree|and tree)"),
    ("the base", r"\bbase\b"),
    ("commands or suite manifest", r"commands?[^.]{0,30}(?:suite manifest|commands)|commands, environment"),
    ("environment and tool versions", r"environment"),
    ("baseline-versus-candidate failures", r"on the base and on the candidate|baseline comparison"),
)

NOT_WEAKENED = r"(?:changes? no|nothing here changes a) (?:board )?authorit"

# Wording that told every reviewer to rerun everything. Each must stay gone.
RETIRED = (
    (BOARD_SKILL, r'"tests pass" is only true if you ran them'),
    (DIRECTOR_GUIDE, r"the implementer's numbers are never evidence"),
    (METHODOLOGY, r"director inspects the result, runs tests, and advances"),
)

FULL_CARRIERS = (
    ("board guide (Audit onboarding)", BOARD_GUIDE),
    ("board skill (Audit prompt)", BOARD_SKILL),
    ("Director guide", DIRECTOR_GUIDE),
    ("Director skill", DIRECTOR_SKILL),
)


def check(condition: object, detail: str) -> None:
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).lower()


def _missing(text: str, clauses) -> list[str]:
    return [label for label, pattern in clauses if not re.search(pattern, text)]


def _section(path: Path, heading: str, next_heading: str) -> str:
    flat = _flat(path)
    start = flat.find(heading.lower())
    check(start >= 0, f"{path.name} has no section {heading!r}")
    end = flat.find(next_heading.lower(), start + len(heading))
    return flat[start:end if end > start else len(flat)]


# --- the policy, in each body a role reads ----------------------------------------


def test_each_review_carrier_states_the_default_and_the_recorded_reason() -> None:
    for label, path in FULL_CARRIERS:
        check(_missing(_flat(path), DEFAULT) == [], f"{label} does not state: {_missing(_flat(path), DEFAULT)}")


def test_each_carrier_states_the_default_for_its_own_reviewer() -> None:
    """The board guide also states the Director's default; that must not stand in for Audit's."""
    own = (
        ("board guide (Audit onboarding)", BOARD_GUIDE, "not rerun the complete suite merely because the ticket entered audit"),
        ("board skill (Audit prompt)", BOARD_SKILL, "not rerun that complete suite merely because the ticket entered their stage"),
        ("Director guide", DIRECTOR_GUIDE, "not rerun the complete suite merely because the ticket entered final sign-off"),
        ("Director skill", DIRECTOR_SKILL, "not rerun the complete suite merely because the ticket entered final sign-off"),
    )
    for label, path, sentence in own:
        check(sentence in _flat(path), f"{label} does not state its own reviewer's default: {sentence!r}")


def test_each_review_carrier_states_the_concrete_triggers() -> None:
    for label, path in FULL_CARRIERS:
        check(_missing(_flat(path), TRIGGERS) == [], f"{label} lacks triggers: {_missing(_flat(path), TRIGGERS)}")


def test_each_review_carrier_says_what_is_not_a_reason() -> None:
    for label, path in FULL_CARRIERS:
        check(_missing(_flat(path), NOT_REASONS) == [],
              f"{label} does not rule out: {_missing(_flat(path), NOT_REASONS)}")


def test_audit_and_director_onboarding_each_carry_every_example() -> None:
    for label, path in (("board guide", BOARD_GUIDE), ("Director guide", DIRECTOR_GUIDE)):
        check(_missing(_flat(path), EXAMPLES) == [], f"{label} lacks examples: {_missing(_flat(path), EXAMPLES)}")
    # The skill is compact: it names every example and points at the guide's table.
    skill = _flat(BOARD_SKILL)
    check(_missing(skill, EXAMPLES[1:]) == [] and "board guide's \"test evidence\"" in skill,
          f"board skill does not point at the worked examples: {_missing(skill, EXAMPLES[1:])}")


def test_the_board_guide_examples_are_one_table_under_the_policy() -> None:
    """Every example is a row of the Test evidence table, not prose elsewhere in the guide."""
    section = _section(BOARD_GUIDE, "## Test evidence: reuse what is exact, rerun for a reason", "## The board skill")
    rows = [line for line in BOARD_GUIDE.read_text(encoding="utf-8").splitlines() if line.startswith("| **")]
    for label, pattern in EXAMPLES:
        check(any(re.search(pattern, row.lower()) for row in rows), f"no example row for {label}")
    check(_missing(section, DEFAULT + TRIGGERS + NOT_REASONS) == [],
          f"the Test evidence section lacks: {_missing(section, DEFAULT + TRIGGERS + NOT_REASONS)}")


def test_the_implementer_records_the_evidence_a_reviewer_needs() -> None:
    for label, path in (("board guide", BOARD_GUIDE), ("board skill", BOARD_SKILL),
                        ("Director guide", DIRECTOR_GUIDE), ("Director skill", DIRECTOR_SKILL)):
        flat = _flat(path)
        check("implementer runs the complete validation" in flat, f"{label} does not give the implementer the run")
        check(_missing(flat, IMPLEMENTER_RECORD) == [],
              f"{label}: the implementer's record lacks {_missing(flat, IMPLEMENTER_RECORD)}")


def test_the_reuse_permission_sits_next_to_the_triggers() -> None:
    """A reader stopping early still meets the limits on reuse."""
    for label, path in FULL_CARRIERS:
        flat = _flat(path)
        grant = re.search(DEFAULT[0][1], flat).start()
        limit = re.search(TRIGGERS[0][1], flat).start()
        check(abs(limit - grant) <= 2500, f"{label}: triggers are {abs(limit - grant)} characters from the default")


def test_independent_review_and_authority_are_kept() -> None:
    for label, path in (("board guide", BOARD_GUIDE), ("board skill", BOARD_SKILL)):
        flat = _flat(path)
        check(re.search(NOT_WEAKENED, flat), f"{label} does not say no authority or requirement changes")
        check("not blind trust" in flat or "not trusting conclusions" in flat, f"{label} reads as blind trust")
        check("what you reused" in flat, f"{label} does not split what was run from what was reused")
    check("kill a mutant of your own" in _flat(DIRECTOR_GUIDE), "the Director guide lost its own mutation")


def test_the_retired_rerun_everything_wording_stays_gone() -> None:
    for path, pattern in RETIRED:
        check(not re.search(pattern, _flat(path)), f"{path.name} still says: {pattern}")


def test_shared_methodology_and_indexes_carry_the_rule() -> None:
    methodology = _flat(METHODOLOGY)
    check("independent *judgment*, not a duplicated computation" in methodology,
          "the methodology still implies every critic reruns everything")
    check("runs focused checks of its own" in methodology, "the methodology's Director step reruns tests")
    librarian = _flat(LIBRARIAN_GUIDE)
    check("prose that nothing executes changes no test result" in librarian
          and "reason to rerun the suites that read it" in librarian,
          "the Librarian guide does not say when a documentation commit needs a rerun")
    operations = _flat(LLM_OPERATIONS)
    check("merely because the ticket entered its stage" in operations and "recorded on the ticket first" in operations,
          "llm-operations does not index the rerun rule")
    check("#test-evidence-reuse-what-is-exact-rerun-for-a-reason" in _flat(DOCUMENTATION_MAP),
          "the documentation map does not route to the Test evidence section")


# --- the carriers that deliver it ---------------------------------------------------


def test_the_onboarding_packet_and_skill_set_ship_these_carriers() -> None:
    names = set(project_onboarding.SWITCHYARD_ONBOARDING_DOC_NAMES)
    for path in (BOARD_GUIDE, DIRECTOR_GUIDE, METHODOLOGY, LIBRARIAN_GUIDE):
        check(path.name in names, f"{path.name} is not in the provisioned onboarding packet")
    shipped = {ROOT / skill.relative_source for skill in board_skill.CANONICAL_SKILLS}
    check({BOARD_SKILL, DIRECTOR_SKILL} <= shipped, "a policy skill is not a canonical skill")


def test_the_policy_reaches_every_installed_board_skill() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd720-skill.") as tmp:
        home = Path(tmp)
        tree = home / "release-c0ffee"
        for skill in board_skill.CANONICAL_SKILLS:
            target = tree / skill.relative_source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / skill.relative_source).read_bytes())
        (tree / board_skill.RELEASE_MARKER_NAME).write_text(json.dumps({"commit": "c0ffee"}) + "\n", encoding="utf-8")
        results = board_skill.install_board_skill(home=home, source=board_skill.default_source(tree))
        check([result.action for result in results] == ["installed"] * len(results) and results,
              f"board skill install: {[result.action for result in results]}")
        for runtime, path in board_skill.adapter_paths(home, board_skill.BOARD_SKILL):
            flat = _flat(path)
            check(_missing(flat, DEFAULT + TRIGGERS) == [], f"{runtime} board skill lacks {_missing(flat, DEFAULT + TRIGGERS)}")


def main() -> int:
    run_module_tests(globals())
    print("review_evidence_policy_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
