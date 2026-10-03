#!/usr/bin/env python3
"""SYRD-539: the opt-in pull scheduling policy is declared, validated, and refused when incoherent.

`scheduling` is absent on every tenant today, and absent keeps Director
dispatch. A tenant that opts in declares where admitted work waits, the one
implementer transition that claims it, and the review stage whose approval
releases the author. Validation never fills anything in, so revalidating a
stored document changes nothing.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import workflow_config  # noqa: E402

CHECKS = 0
EXAMPLE = ROOT / "examples" / "workflows" / "inspection.json"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def pull_document(**policy) -> dict:
    """The shipped example, opted in: a ready stage the Director admits to, and an implementer claim out of it."""
    doc = json.loads(EXAMPLE.read_text())
    analysis = next(s for s in doc["stages"] if s["name"] == "analysis")
    doc["stages"].insert(doc["stages"].index(analysis) + 1, {
        **copy.deepcopy(analysis), "name": "ready", "label": "Ready", "owners": [],
        "notify": {"kind": "none", "role": None},
    })
    template = next(t for t in doc["transitions"] if t["action"] == "route" and t["from"] == "analysis")
    implementers = sorted(next(s for s in doc["stages"] if s["kind"] == "implementation")["owners"])
    doc["transitions"] += [
        {**copy.deepcopy(template), "from": "analysis", "to": "ready", "action": "admit", "label": "Admit"},
        {**copy.deepcopy(template), "from": "ready", "to": "in_progress", "action": "claim", "label": "Claim",
         "actors": implementers},
        {**copy.deepcopy(template), "from": "ready", "to": "backlog", "action": "defer", "label": "Defer"},
        {**copy.deepcopy(template), "from": "ready", "to": "analysis", "action": "withdraw", "label": "Withdraw"},
        # The Director can still start admitted work on someone directly: an
        # ordinary transition between the same stages, not the claim.
        {**copy.deepcopy(template), "from": "ready", "to": "in_progress", "action": "director_start", "label": "Start"},
    ]
    doc["queue"] = {"stage": "ready", "assignee": "unassigned"}
    doc["scheduling"] = {"mode": "pull", "ready_stage": "ready", "release_after": "audit", **policy}
    return doc


def refused(document: dict) -> str:
    try:
        workflow_config.validate(document)
    except ValueError as exc:
        return str(exc)
    return ""


def test_absent_keeps_every_tenant_as_it_is() -> None:
    doc = json.loads(EXAMPLE.read_text())
    check("scheduling" not in workflow_config.validate(doc), "absent stays absent: Director dispatch, unchanged")


def test_a_coherent_pull_policy_validates_and_is_not_rewritten() -> None:
    doc = pull_document()
    validated = workflow_config.validate(doc)
    check(validated["scheduling"] == doc["scheduling"],
          f"nothing is filled in, so a revalidated document does not change: {validated['scheduling']}")
    check(workflow_config.validate(pull_document(idle_alert_seconds=600))["scheduling"]["idle_alert_seconds"] == 600,
          "an explicit idle alert interval is kept")
    check("ready" not in workflow_config.parking_stage_names(validated)
          and "backlog" in workflow_config.parking_stage_names(validated),
          "the ready stage has a parking stage's shape but is not one: admitted work there is never parked")
    no_policy = pull_document()
    no_policy.pop("scheduling")
    check("ready" in workflow_config.parking_stage_names(no_policy),
          "the same stage without the policy is ordinary parking")


def test_incoherent_policies_are_refused() -> None:
    cases = {
        "an unknown key": (lambda d: d["scheduling"].update(extra=1), "invalid scheduling policy"),
        "a missing release point": (lambda d: d["scheduling"].pop("release_after"), "invalid scheduling policy"),
        "an unknown mode": (lambda d: d["scheduling"].update(mode="push"), "invalid scheduling mode"),
        "lifecycle reservation too": (lambda d: d.update(reservation="lifecycle"),
                                      "scheduling cannot be combined with reservation lifecycle"),
        "an undeclared ready stage": (lambda d: d["scheduling"].update(ready_stage="nowhere"),
                                      "scheduling ready stage must be a declared stage"),
        "the implementation stage as ready": (lambda d: d["scheduling"].update(ready_stage="in_progress"),
                                              "scheduling ready stage must be a non-terminal holding stage"),
        "a review stage as the release point's opposite": (lambda d: d["scheduling"].update(release_after="analysis"),
                                                           "scheduling release_after must be a review stage"),
        "an alert interval out of range": (lambda d: d["scheduling"].update(idle_alert_seconds=5),
                                           "scheduling idle_alert_seconds must be an integer from 60 to 86400"),
        "a boolean alert interval": (lambda d: d["scheduling"].update(idle_alert_seconds=True),
                                     "scheduling idle_alert_seconds must be an integer from 60 to 86400"),
    }
    for label, (mutate, message) in cases.items():
        doc = pull_document()
        mutate(doc)
        check(refused(doc) == message, f"{label}: refused as {message!r}, got {refused(doc)!r}")

    doc = pull_document()
    next(t for t in doc["transitions"] if t["action"] == "admit")["actors"] = ["director", "main"]
    check(refused(doc) == "only the director may admit work to the ready stage",
          f"an implementer cannot admit its own work: {refused(doc)!r}")
    doc = pull_document()
    doc["transitions"] = [t for t in doc["transitions"] if t["action"] != "withdraw"]
    check(refused(doc) == "the director must be able to withdraw admitted work from the ready stage",
          f"no way to withdraw admitted work: {refused(doc)!r}")
    doc = pull_document()
    doc["queue"] = {"stage": "backlog", "assignee": "unassigned"}
    check(refused(doc) == "scheduling needs the queue to be the ready stage",
          f"pinned rework must wait in the ready stage: {refused(doc)!r}")
    doc = pull_document()
    doc["transitions"] = [t for t in doc["transitions"] if t["action"] != "claim"]
    check(refused(doc) == "scheduling needs exactly one implementer move from the ready stage into implementation",
          f"no claim transition: {refused(doc)!r}")
    doc = pull_document()
    next(t for t in doc["transitions"] if t["action"] == "director_start")["require_reason"] = True
    check(refused(doc) == "", f"a Director's own move between the same stages may require a reason: {refused(doc)!r}")
    for field in ("require_reason", "owner_scoped"):
        doc = pull_document()
        next(t for t in doc["transitions"] if t["action"] == "claim")[field] = True
        check(refused(doc) == "the claim transition cannot require a reason or be owner-scoped: a pulled claim has neither",
              f"a claim with {field} is one the board could not take honestly: {refused(doc)!r}")
    doc = pull_document()
    next(t for t in doc["transitions"] if t["action"] == "claim")["actors"] = ["director"]
    check(refused(doc) == "scheduling needs exactly one implementer move from the ready stage into implementation",
          f"a Director-only claim is dispatch, not pull: {refused(doc)!r}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"pull_scheduling_policy_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
