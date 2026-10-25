"""The board writes convergence makes, as the Director (SYRD-628).

`switchyard worker-pool converge` records every start, stop and deferral, and
brackets each stop with a drain so no claim lands on a worker being stopped.
"""

from __future__ import annotations

from typing import Any

from .write_client import TicketBoardWriteClient


class CohortWriter(TicketBoardWriteClient):
    def record_cohort_change(self, runtime: str, role: str, *, kind: str, source: str, reason: str,
                             detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record a convergence start, stop, deferral or failure."""
        payload = {"runtime": runtime, "role": role, "kind": kind, "source": source, "reason": reason, "detail": detail or {}}
        return self._post("/actions/record_cohort_change", payload)

    def begin_cohort_drain(self, role: str, *, reason: str, seconds: int = 600) -> dict[str, Any]:
        """Refuse claims for `role` while it is stopped; says what it already holds or has pinned to it."""
        return self._post("/actions/begin_cohort_drain", {"role": role, "reason": reason, "seconds": seconds})

    def end_cohort_drain(self, role: str) -> dict[str, Any]:
        return self._post("/actions/end_cohort_drain", {"role": role})
