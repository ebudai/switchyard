"""What a worker-pool start refuses, asked by start and, before it stops anything, by restart (SYRD-635).

A restart stops the live worker and then starts it. When the start half was
going to be refused, the stop left the worker down for nothing: the Director
asked for a fresh worker and got none. So restart asks this first, and a
refusal leaves the worker exactly as it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence


def start_refusal(pool, member: str, *, readiness: Sequence[Any], config_path: Path, force: bool) -> tuple[Any, str]:
    """(the member's readiness, why starting it would be refused, or "")."""
    from scripts.worker_pool import PREPARATION_BLOCKERS, prepare_role_command

    state = next((candidate for candidate in readiness if candidate.role == member), None)
    if state is None:
        raise ValueError(f"{member} is not a live member of the {pool.name} pool")
    if not state.blockers or force:
        return state, ""
    detail = "; ".join(state.blockers)
    if any(blocker in state.blockers for blocker in PREPARATION_BLOCKERS):
        # Preparation is its own supported step; start does not do it.
        detail += f". Prepare it first, then start it again: {prepare_role_command(config_path, member)}"
    return state, detail
