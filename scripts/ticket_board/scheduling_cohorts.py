"""Provider cohorts in a pull pool (SYRD-628).

A cohort is scheduling capacity: the claimant pool's members of one runtime,
declared in the workflow's `scheduling.cohorts`, with

* `target`: how many of them should be running;
* `ceiling`: how many tickets they may hold at once -- the board refuses a
  claim past it, on every claim path;
* `model` and `effort`: what its workers are pinned to. Convergence only starts
  a worker whose own configuration matches; it never rewrites one.

The board's copy of these rules is `ticket_board.pull_cohort`; the two are
tested to agree.
"""

from __future__ import annotations

from typing import Any, Mapping

COHORT_FIELDS = frozenset({"runtime", "target", "ceiling", "model", "effort"})
COHORT_COUNT_RANGE = (0, 64)
COHORT_NAME_CHARS = 120


def validate_cohorts(cohorts: Any, need: Any, *, runtimes: set[str]) -> None:
    need(isinstance(cohorts, list) and bool(cohorts), "scheduling cohorts must be a non-empty list")
    seen: set[str] = set()
    for cohort in cohorts:
        need(isinstance(cohort, dict) and set(cohort) <= COHORT_FIELDS and "runtime" in cohort,
             "invalid scheduling cohort")
        need(cohort["runtime"] in runtimes and cohort["runtime"] not in seen,
             "a scheduling cohort names a known runtime, and each runtime once")
        seen.add(cohort["runtime"])
        need("target" in cohort or "ceiling" in cohort, "a scheduling cohort needs a target or a ceiling")
        for key in ("target", "ceiling"):
            value = cohort.get(key, 0)
            need(isinstance(value, int) and not isinstance(value, bool)
                 and COHORT_COUNT_RANGE[0] <= value <= COHORT_COUNT_RANGE[1],
                 f"scheduling cohort {key} must be an integer from 0 to 64")
        for key in ("model", "effort"):
            value = cohort.get(key, "x")
            need(isinstance(value, str) and value == value.strip() and 0 < len(value) <= COHORT_NAME_CHARS,
                 f"scheduling cohort {key} must be a name of 1 to 120 characters")


def cohorts(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The declared cohorts, in declaration order; [] without a pull policy or cohorts."""
    policy = cfg.get("scheduling") if isinstance(cfg, Mapping) else None
    return [dict(c) for c in (policy or {}).get("cohorts") or [] if isinstance(c, Mapping)]


def cohort_of(cfg: Mapping[str, Any], role: str) -> dict[str, Any] | None:
    """The cohort a claimant belongs to: the entry for its declared runtime, or None."""
    from .workflow_config import pull_claimant_pool

    if role not in pull_claimant_pool(cfg):
        return None
    runtime = next((r.get("runtime") for r in cfg.get("roles") or [] if r.get("name") == role), None)
    return next((c for c in cohorts(cfg) if c.get("runtime") == runtime), None)
